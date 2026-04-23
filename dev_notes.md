# Developer Notes — OmniStream Studio

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│                   PyQt6 GUI                      │
│  ┌──────────────┐  ┌──────────────────────────┐ │
│  │ DrawingCanvas │  │     SettingsPanel        │ │
│  │  (QWidget)    │  │  (scrollable side panel) │ │
│  │               │  │                          │ │
│  │ - QImage      │  │ - Drawing tools          │ │
│  │ - bg_image    │  │ - Stream settings        │ │
│  │ - overlays    │  │                          │ │
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
│                  StreamThread (QThread)            │
│                                                    │
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

### Framework: PyQt6

As of the v2 rewrite, the app uses **PyQt6 6.11.0** with **PyQt6-WebEngine 6.11.0** (Chromium-based). Key API changes from PyQt5:

- Enums are now scoped (e.g., `Qt.Horizontal` → `Qt.Orientation.Horizontal`, `Qt.SolidLine` → `Qt.PenStyle.SolidLine`)
- `QImage.Format` values accessed via `QImage.Format.Format_ARGB32`
- `QPainter.RenderHint` enum class for render hints
- `app.exec_()` → `app.exec()`
- `QUndoStack` / `QUndoCommand` moved from `QtWidgets` to `QtGui`
- `QWebEnginePage` and `QWebEngineProfile` live in `QtWebEngineCore` (not `QtWebEngineWidgets`)
- `QImage.bits()` returns `sip.voidptr` — use `ctypes` to extract bytes: `(ctypes.c_char * size).from_address(ptr)[:]`
- `QSizePolicy.Expanding` → `QSizePolicy.Policy.Expanding`
- `QLineEdit.Password` → `QLineEdit.EchoMode.Password`
- `QTextCursor.End` → `QTextCursor.MoveOperation.End`

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

## Live Browser Sources (QtWebEngine)

Browser sources render live HTML/JavaScript web pages directly on the canvas using `QWebEngineView`. Each browser source:

- Creates its own hidden `QWebEngineView` with `WA_DontShowOnScreen` attribute
- Captures frames on-demand during `paintEvent` at configurable fps (default: 15) via `view.render(&painter)` into `QImage`
- Sets transparent page backgrounds via JavaScript injection (`document.body.style.setProperty('background-color', 'transparent', 'important')`)
- Supports drag, select, and reposition on the canvas
- Displays status indicators: "Initializing...", "Loading...", or "Failed: <url>"
- Is cleaned up properly via `stop()` on removal
- Debug frames saved to `logs/browser_capture.png` for diagnostics

### Implementation Notes

- Uses `QGraphicsScene` + `QGraphicsView` approach for offscreen Chromium rendering
- Frame capture uses `QImage` with `Format_ARGB32` for transparency support
- `ctypes` extracts raw bytes from `QImage.constBits()` for FFmpeg streaming (`get_frame_bytes()`)
- Rate-limited capture: won't re-capture more than once per `capture_interval` ms
- Transparent HTML backgrounds require both `document.body` AND `document.documentElement` CSS

**Performance note**: Each browser source runs a separate Chromium process. Keep the number of simultaneous browser sources low (1-3 recommended) to avoid memory/CPU pressure.

**Known issues**:
- Some HTTPS sites may block automated rendering (Google works, some sites have CSP restrictions)
- `file:///` URLs may not render in all cases — prefer HTTPS URLs
- Very complex pages may cause frame drops — consider simpler pages for overlays

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