# OmniStream Studio

A desktop app for drawing on a canvas, adding images, text, and live web pages — and streaming everything live to Twitch, Kick, YouTube, or any RTMP platform — powered by your AMD GPU.

## Features

- **Drawing Canvas** — Freehand drawing with adjustable brush size and color, plus an eraser tool
- **Image Overlays** — Import images, drag them around, and resize them on the canvas
- **Text Overlays** — Add text with custom font, size, and color
- **Live Browser Sources** — Render live HTML/JavaScript web pages directly on the canvas using QtWebEngine (Chromium)
- **Background Image** — Set a full-canvas background image that scales to match your resolution
- **Two Resolutions** — Switch between 1920×1080 (1080p) and 1280×720 (720p)
- **GPU-Accelerated Streaming** — Uses your AMD GPU (VAAPI) for zero-CPU encoding
- **Multi-Platform** — Stream to Twitch, Kick, YouTube, Facebook, or any custom RTMP server
- **Configurable** — Bitrate, FPS, and encoder settings are saved automatically

## Requirements

- Linux (Arch Linux recommended)
- AMD GPU with VAAPI support
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- FFmpeg installed on your system
- Qt6 WebEngine (installed automatically via `uv sync`)

## Quick Start

```bash
# Clone or navigate to the project
cd OmniStreamStudioPy

# Install dependencies (includes PyQt6 and PyQt6-WebEngine)
uv sync

# Run the app
uv run python main.py
```

## Usage

### Drawing
- Select a brush color using the color picker in the side panel
- Adjust brush size with the slider
- Toggle the eraser to remove parts of your drawing
- Click "Clear Canvas" to start fresh

### Adding Media
- **Import Image** — Opens a file picker. Selected images appear centered on the canvas. Click and drag to move them. Drag the bottom-right corner to resize.
- **Add Text** — Opens a dialog where you can type text, choose font, size, and color. Click and drag to reposition.

### Browser Sources
Browser sources render live web pages (with HTML and JavaScript) directly on the canvas using Chromium. Each browser source captures frames at 15 fps and supports transparent backgrounds.

1. Click **Add Browser Source** in the Resources panel
2. Enter a URL (e.g., `https://www.google.com`)
3. The page loads and renders live on the canvas
4. Click and drag to reposition; the dashed border indicates selection
5. Double-click in the Resources list to edit or remove

**Tips**:
- Most HTTPS websites work well (Google, news sites, dashboards)
- Some sites may block automated rendering due to security policies
- Browser sources with transparent backgrounds overlay on your drawing/background
- Check `logs/browser_capture.png` for debug captures if a source isn't rendering

### Streaming
1. Open the **Stream Settings** panel
2. Select your platform (Twitch, Kick, YouTube, Facebook, or Custom)
3. Enter your stream key
4. Choose your encoder:
   - **VAAPI (AMD GPU)** — Recommended, uses your GPU for encoding
   - **AMF (AMD GPU)** — Alternative AMD encoder
   - **x264 (CPU)** — Falls back to CPU encoding
5. Set bitrate (default: 4000 kbps) and FPS (default: 30)
6. Click **Start Stream**

Your settings are saved automatically and restored the next time you open the app.

### Logs
Two log panels sit below the canvas:
- **App Log** — Shows what the app is doing
- **FFmpeg Log** — Shows encoder and streaming output

Log files are also saved in the `logs/` folder.

## Testing

Run a local encoding test (no RTMP output):

```bash
uv run python test_stream.py --fps-test --duration 5 --fps 30
```

Stream to your platform for a set duration:

```bash
uv run python test_stream.py --duration 60
```

## Tech Stack

- **PyQt6 6.11.0** — GUI framework
- **PyQt6-WebEngine** — Chromium-based browser rendering for live web overlays
- **libavformat + VAAPI** — GPU-accelerated RTMP streaming via custom C extension
- **Python 3.12** — Application logic

## Project Structure

| File | Description |
|---|---|
| `main.py` | Main application (GUI, canvas, streaming) |
| `omnistream.c` | C extension for GPU-accelerated RTMP streaming |
| `setup_ext.py` | Build script for the C extension |
| `test_stream.py` | Test script for encoding and streaming |
| `style.qss` | Application stylesheet |
| `data.json` | Your saved settings (auto-generated, not tracked in git) |
| `assets/` | Background images |
| `logs/` | Log files (auto-generated) |

## License

MIT