#!/usr/bin/env python3
"""Test script to verify the C extension RTMP streaming works.

Usage:
    uv run python test_stream.py                  # uses values from data.json
    uv run python test_stream.py --key YOUR_KEY   # override stream key
    uv run python test_stream.py --platform kick  # override platform
    uv run python test_stream.py --duration 10    # stream for 10 seconds then stop
    uv run python test_stream.py --fps-test       # encode-only benchmark (no RTMP)
"""

import sys
import os
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(__file__))

DATA_PATH = os.path.join(os.path.dirname(__file__), "data.json")

PLATFORM_URLS = {
    "twitch": "rtmp://live.twitch.tv/app",
    "kick": "rtmp://stream.kick.com/live",
    "youtube": "rtmp://a.rtmp.youtube.com/live2",
    "facebook": "rtmps://live-api-s.facebook.com:443/rtmp/",
}

def load_config():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH) as f:
            return json.load(f)
    return {}

def generate_test_frame(width, height, frame_num):
    """Generate a BGRA test frame with animated color bars using numpy for speed."""
    import numpy as np

    bar_count = 8
    bar_width = width // bar_count

    colors = np.array([
        [255, 255, 255, 255], [0, 255, 255, 255], [255, 255, 0, 255], [0, 255, 0, 255],
        [255, 0, 255, 255], [0, 0, 255, 255], [255, 0, 0, 255], [0, 0, 0, 255]
    ], dtype=np.uint8)

    bar_indices = np.arange(width) // bar_width
    row = colors[bar_indices]
    frame = np.tile(row[np.newaxis, :, :], (height, 1, 1))

    shift = (frame_num * 3) % 256
    frame[:, :, 0] = np.clip(frame[:, :, 0].astype(np.int16) + shift, 0, 255).astype(np.uint8)
    frame[:, :, 1] = np.clip(frame[:, :, 1].astype(np.int16) - shift // 2, 0, 255).astype(np.uint8)

    return frame.tobytes()

def test_fps(width, height, fps, duration):
    """Test encoding speed without RTMP output."""
    try:
        import omnistream
    except ImportError as e:
        print(f"ERROR: Cannot import omnistream C extension: {e}")
        print("Build it first: uv run python setup_ext.py build_ext --inplace")
        sys.exit(1)

    print(f"Testing VAAPI encoding performance (no RTMP)...")
    print(f"  Resolution: {width}x{height}")
    print(f"  FPS target: {fps}")
    print(f"  Duration: {duration}s")
    print()

    try:
        streamer = omnistream.Streamer(
            url="/dev/null",
            width=width,
            height=height,
            fps=fps,
            bitrate=4000
        )
    except RuntimeError as e:
        print(f"ERROR: Failed to initialize streamer: {e}")
        sys.exit(1)

    total_frames = fps * duration
    frame = generate_test_frame(width, height, 0)

    print(f"Sending {total_frames} frames (no sleep)...")
    start = time.time()
    for i in range(total_frames):
        frame = generate_test_frame(width, height, i)
        success = streamer.send_frame(frame)
        if not success:
            print(f"ERROR: Frame {i} failed: {streamer.get_error()}")
            break
    encode_time = time.time() - start

    streamer.flush()
    flush_time = time.time() - start

    actual_fps = total_frames / encode_time
    print()
    print(f"Results:")
    print(f"  Frames sent: {total_frames}")
    print(f"  Encode time: {encode_time:.3f}s")
    print(f"  Encode speed: {actual_fps:.1f} fps")
    print(f"  Total time (with flush): {flush_time:.3f}s")
    print(f"  Target FPS: {fps}")
    if actual_fps >= fps:
        print(f"  Status: PASS (can sustain {fps} fps)")
    else:
        print(f"  Status: FAIL (below {fps} fps target)")

def test_rtmp(url, stream_key, width, height, fps, bitrate, duration):
    """Test actual RTMP streaming."""
    try:
        import omnistream
    except ImportError as e:
        print(f"ERROR: Cannot import omnistream C extension: {e}")
        print("Build it first: uv run python setup_ext.py build_ext --inplace")
        sys.exit(1)

    stream_url = f"{url}/{stream_key}"
    print(f"Testing RTMP stream...")
    print(f"  URL: {url}/***")
    print(f"  Resolution: {width}x{height}")
    print(f"  FPS: {fps}")
    print(f"  Bitrate: {bitrate}kbps")
    print(f"  Duration: {duration}s")
    print()

    try:
        print("Initializing VAAPI encoder...")
        streamer = omnistream.Streamer(
            url=stream_url,
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate
        )
        print("Encoder initialized successfully")
    except RuntimeError as e:
        print(f"ERROR: Failed to initialize streamer: {e}")
        sys.exit(1)

    total_frames = fps * duration
    bytes_sent = 0

    print(f"Sending {total_frames} frames...")
    start = time.time()

    try:
        for i in range(total_frames):
            frame = generate_test_frame(width, height, i)
            success = streamer.send_frame(frame)
            if not success:
                err = streamer.get_error()
                print(f"ERROR: Frame {i} failed: {err}")
                break
            bytes_sent += len(frame)

            if (i + 1) % fps == 0:
                elapsed = time.time() - start
                print(f"  Second {i // fps + 1}: {i + 1} frames, {bytes_sent / 1024 / 1024:.1f}MB encoded, {elapsed:.1f}s elapsed")

            time.sleep(1.0 / fps)
    except KeyboardInterrupt:
        print("\nInterrupted by user")

    elapsed = time.time() - start
    print()
    print("Flushing encoder...")
    streamer.flush()

    print()
    print(f"Stream complete:")
    print(f"  Frames sent: {total_frames}")
    print(f"  Total data: {bytes_sent / 1024 / 1024:.1f}MB")
    print(f"  Duration: {elapsed:.1f}s")
    print(f"  Average bitrate: {bytes_sent * 8 / elapsed / 1000:.0f}kbps")

def main():
    parser = argparse.ArgumentParser(description="Test OmniStream C extension")
    parser.add_argument("--key", help="Stream key (overrides data.json)")
    parser.add_argument("--platform", choices=["twitch", "kick", "youtube", "facebook"],
                       help="Platform (overrides data.json)")
    parser.add_argument("--url", help="Custom RTMP URL")
    parser.add_argument("--width", type=int, default=None, help="Canvas width")
    parser.add_argument("--height", type=int, default=None, help="Canvas height")
    parser.add_argument("--fps", type=int, default=None, help="Target FPS")
    parser.add_argument("--bitrate", type=int, default=None, help="Bitrate in kbps")
    parser.add_argument("--duration", type=int, default=10, help="Test duration in seconds")
    parser.add_argument("--fps-test", action="store_true",
                       help="Test encoding speed only (no RTMP output)")
    args = parser.parse_args()

    config = load_config()

    width = args.width or (1920 if "1920" in config.get("resolution", "1920x1080") else 1280)
    height = args.height or (1080 if "1920" in config.get("resolution", "1920x1080") else 720)
    fps = args.fps or int(config.get("fps", "30"))
    bitrate = args.bitrate or config.get("bitrate", 4000)

    if args.fps_test:
        test_fps(width, height, fps, args.duration)
        return

    stream_key = args.key or config.get("stream_key", "")
    if not stream_key:
        print("ERROR: No stream key provided.")
        print("Set it in data.json or use --key YOUR_KEY")
        sys.exit(1)

    if args.url:
        rtmp_url = args.url
    elif args.platform:
        rtmp_url = PLATFORM_URLS[args.platform]
    else:
        platform = config.get("platform", "twitch").lower()
        rtmp_url = config.get("rtmp_url", PLATFORM_URLS.get(platform, "rtmp://live.twitch.tv/app"))

    if not rtmp_url:
        print("ERROR: No RTMP URL. Use --url or --platform")
        sys.exit(1)

    test_rtmp(rtmp_url, stream_key, width, height, fps, bitrate, args.duration)

if __name__ == "__main__":
    main()
