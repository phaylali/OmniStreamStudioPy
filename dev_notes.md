# Developer Notes — OmniStream Studio

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│                   PyQt5 GUI                      │
│  ┌──────────────┐  ┌──────────────────────────┐ │
│  │ DrawingCanvas │  │     SettingsPanel        │ │
│  │  (QWidget)    │  │  (scrollable side panel) │ │
│  │               │  │                          │ │
│  │ - QImage      │  │ - Drawing tools          │ │
│  │ - bg_image    │  │ - Media (images, text)   │ │
│  │ - overlays    │  │ - Stream settings        │ │
│  └───────┬───────┘  └──────────┬───────────────┘ │
│          │                     │                  │
│          ▼                     ▼                  │
│  ┌──────────────────────────────────────┐        │
│  │          MainWindow                  │        │
│  │  - ConfigManager (JSON, 5s debounce) │        │
│  │  - LogHandler (text widget + file)   │        │
│  └──────────────────┬───────────────────┘        │
└─────────────────────┼────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────┐
│                  StreamThread (QThread)              │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │           C Extension (omnistream.so)          │ │
│  │                                                 │ │
│  │  Python bytes (BGRA, 1920×1080×4)              │ │
│  │         │                                      │ │
│  │         ▼                                      │ │
│  │  ┌─────────────┐    ┌──────────────────────┐  │ │
│  │  │  libswscale │───▶│  BGRA → NV12 convert │  │ │
│  │  └─────────────┘    └──────────┬───────────┘  │ │
│  │                                │               │ │
│  │                                ▼               │ │
│  │  ┌──────────────────────────────────────────┐ │ │
│  │  │  libavcodec (h264_vaapi)                 │ │ │
│  │  │  - VAAPI hw_frames_ctx                   │ │ │
│  │  │  - /dev/dri/renderD128                   │ │ │
│  │  │  - GPU encoding, zero CPU                │ │ │
│  │  └──────────────────┬───────────────────────┘ │ │
│  │                     │                         │ │
│  │                     ▼                         │ │
│  │  ┌──────────────────────────────────────────┐ │ │
│  │  │  libavformat (FLV muxer → RTMP)         │ │ │
│  │  │  - av_interleaved_write_frame()          │ │ │
│  │  │  - Direct TCP to RTMP ingest server      │ │ │
│  │  └──────────────────────────────────────────┘ │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Why a C Extension Instead of FFmpeg Subprocess?

The original implementation piped raw BGRA frames (~8MB/frame at 1080p) through stdin to an FFmpeg subprocess. This had several problems:

1. **stdin pipe overhead** — 240MB/s at 30fps, significant CPU overhead
2. **No error visibility** — FFmpeg stderr was hard to parse and correlate with frame failures
3. **Process lifecycle management** — SIGPIPE, zombie processes, cleanup complexity

The C extension (`omnistream.c`) links directly against:
- `libswscale` — BGRA → NV12 color conversion
- `libavcodec` — h264_vaapi hardware encoding via VAAPI
- `libavformat` — FLV muxer and RTMP output
- `libavutil` — Hardware context management

**Result**: Direct memory-to-encoder pipeline, no subprocess, proper Python exceptions.

### Frame Pipeline

```
QImage (BGRA, 1920×1080)
    │
    ├── QPainter draws background image (if set)
    ├── QPainter draws draggable images
    ├── QPainter draws text overlays
    │
    ▼
QImage.bits() → bytes (8,294,400 bytes per frame)
    │
    ▼
C Extension:
    sws_scale() — BGRA → NV12 (software)
    av_hwframe_transfer_data() — NV12 → VAAPI hw frame
    avcodec_send_frame() / avcodec_receive_packet() — encode
    av_interleaved_write_frame() — mux and RTMP push
```

### Native Resolution vs Display Scaling

The canvas has a **fixed native resolution** (1920×1080 or 1280×720). The display widget scales uniformly to fit the available window space while maintaining 16:9. All coordinates (images, text, drawing) are stored in native resolution space. The stream always outputs the native resolution — nothing gets cut off.

### Config Persistence

`ConfigManager` uses a JSON file (`data.json`) with a 5-second debounce timer. Every config change restarts the timer; the file is only written after 5 seconds of inactivity. On app close, the timer is cancelled and an immediate save is forced.

## Building the C Extension

```bash
uv run python setup_ext.py build_ext --inplace
```

This produces `omnistream.cpython-312-x86_64-linux-gnu.so` in the project root.

### Dependencies (system packages)

- `ffmpeg` (provides libavformat, libavcodec, libswscale, libavutil dev headers)
- `gcc`
- `pkg-config`

### VAAPI Setup

The extension uses `/dev/dri/renderD128` for VAAPI. On Arch Linux with AMD GPUs:

```bash
# Required packages
pacman -S libva mesa libva-mesa-driver

# Verify
vainfo
```

### Encoder Comparison

| Encoder | Init Time | 1080p Encode Speed | CPU Usage | GPU Usage |
|---|---|---|---|---|
| h264_vaapi (C ext) | ~1.2s | 208 fps | ~2% | ~15% |
| h264_vaapi (FFmpeg) | ~0.5s | ~180 fps | ~5% | ~15% |
| h264_amf (FFmpeg) | ~0.8s | ~150 fps | ~8% | ~20% |
| libx264 (FFmpeg) | ~0.3s | ~45 fps | ~60% | 0% |

## Testing

### Null Output Test (encode only, no RTMP)

```bash
uv run python test_stream.py --fps-test --duration 5 --fps 30
```

Uses `/dev/null` as the output URL. The C extension skips muxing when it detects this, only testing the encoder pipeline.

### Real RTMP Test

```bash
uv run python test_stream.py --duration 120
```

Reads URL and stream key from `data.json`. Streams animated color bars.

## Known Limitations

1. **Python frame generation overhead** — Generating test frames in Python (even with numpy) caps at ~55 fps for 1080p. The encoder itself does 208 fps. The bottleneck is Python-side frame creation, not encoding.
2. **Single encoder instance** — Only one stream can run at a time (by design).
3. **No audio** — The app only streams video. Audio would require a separate input source and audio encoder.
4. **VAAPI only on Linux** — The C extension is hardcoded to use `/dev/dri/renderD128`. AMF fallback via FFmpeg subprocess works on Windows.

## Log Files

Logs are written to `logs/omnistream_YYYYMMDD_HHMMSS.log` with timestamps and severity levels. Each app launch creates a new log file. The `LogHandler` class mirrors logs to the in-app text widget in real-time.

## File Not Tracked in Git

- `data.json` — Contains stream keys and user config
- `logs/` — Runtime log files
- `*.so` — Compiled C extension
- `build/` — Build artifacts
