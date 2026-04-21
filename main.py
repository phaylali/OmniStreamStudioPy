import sys
import os
import json
import subprocess
import threading
import time
import logging
from datetime import datetime
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QSlider, QColorDialog,
                             QFileDialog, QFrame, QComboBox, QLineEdit, QGroupBox,
                             QSplitter, QSizePolicy, QSpinBox, QCheckBox, QScrollArea,
                             QMessageBox, QFormLayout, QTextEdit, QTabWidget)
from PyQt5.QtCore import Qt, QTimer, QPoint, QRect, pyqtSignal, QThread, QSize, QIODevice
from PyQt5.QtGui import (QPainter, QPen, QColor, QImage, QPixmap, QFont, 
                         QBrush, QMouseEvent, QDragEnterEvent, QDropEvent,
                         QTransform, QFontMetrics, QTextCursor)

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, f"omnistream_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("OmniStream")

DEFAULT_CONFIG = {
    "resolution": "1920x1080 (1080p)",
    "bg_color": [30, 30, 30],
    "brush_color": [255, 255, 255],
    "brush_size": 5,
    "encoder": "VAAPI (AMD GPU)",
    "platform": "Twitch",
    "rtmp_url": "rtmp://live.twitch.tv/app",
    "stream_key": "",
    "bitrate": 4000,
    "fps": "30"
}

class ConfigManager:
    def __init__(self, path=None):
        self.path = path or os.path.join(os.path.dirname(__file__), "data.json")
        self.config = {}
        self._pending_changes = False
        self._save_timer = None
        self._load()
        
    def _load(self):
        logger.debug("ConfigManager._load: path=%s", self.path)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    self.config = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in self.config:
                        logger.debug("ConfigManager._load: missing key '%s', setting default", k)
                        self.config[k] = v
                logger.info("ConfigManager._load: loaded %d keys from existing file", len(self.config))
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("ConfigManager._load: failed to parse file (%s), using defaults", e)
                self.config = DEFAULT_CONFIG.copy()
                self._save_now()
        else:
            logger.info("ConfigManager._load: no config file found, creating with defaults")
            self.config = DEFAULT_CONFIG.copy()
            self._save_now()
            
    def _save_now(self):
        logger.debug("ConfigManager._save_now: writing %d keys to %s", len(self.config), self.path)
        with open(self.path, "w") as f:
            json.dump(self.config, f, indent=2)
        logger.info("ConfigManager._save_now: config saved")
            
    def get(self, key, default=None):
        val = self.config.get(key, default)
        logger.debug("ConfigManager.get: key='%s' -> %s", key, repr(val) if key != 'stream_key' else '***')
        return val
        
    def set(self, key, value):
        if isinstance(value, str):
            value = value.strip()
        if self.config.get(key) != value:
            logger.debug("ConfigManager.set: key='%s' changed", key)
            self.config[key] = value
            self._pending_changes = True
            self._schedule_save()
            
    def _schedule_save(self):
        if self._save_timer:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(5.0, self._save_now)
        self._save_timer.daemon = True
        self._save_timer.start()
        logger.debug("ConfigManager._schedule_save: 5s debounce timer started")
        
    def save(self):
        logger.debug("ConfigManager.save: forcing immediate save")
        if self._save_timer:
            self._save_timer.cancel()
        self._save_now()


class LogHandler(logging.Handler):
    def __init__(self, text_edit):
        super().__init__()
        self.text_edit = text_edit
        self.setLevel(logging.DEBUG)
        
    def emit(self, record):
        msg = self.format(record)
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(msg + "\n")
        self.text_edit.setTextCursor(cursor)
        self.text_edit.ensureCursorVisible()


class StreamThread(QThread):
    status_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str)
    log_signal = pyqtSignal(str)
    
    def __init__(self, canvas_width, canvas_height, rtmp_url, stream_key, bitrate=4000, fps=30, encoder="vaapi"):
        super().__init__()
        self.canvas_width = canvas_width
        self.canvas_height = canvas_height
        self.rtmp_url = rtmp_url
        self.stream_key = stream_key
        self.bitrate = bitrate
        self.fps = fps
        self.encoder = encoder
        self.running = False
        self.frame_data = None
        self._stop_event = threading.Event()
        logger.info("StreamThread.__init__: %dx%d encoder=%s bitrate=%dk fps=%d url=%s",
                    canvas_width, canvas_height, encoder, bitrate, fps, rtmp_url)
        
    def set_frame(self, frame_bytes):
        self.frame_data = frame_bytes
        
    def run(self):
        logger.info("StreamThread.run: starting")
        self.running = True
        stream_url = self.rtmp_url.rstrip('/') + '/' + self.stream_key
        logger.debug("StreamThread.run: full stream URL = %s/***", self.rtmp_url)
        
        if self.encoder == "vaapi":
            try:
                import omnistream
            except ImportError as e:
                logger.error("StreamThread.run: failed to import C extension: %s", e)
                self.error_signal.emit(f"Failed to load streaming extension: {e}")
                return

            logger.info("StreamThread.run: using native C extension (libavformat + VAAPI)")
            try:
                self.status_signal.emit("Starting stream...")
                streamer = omnistream.Streamer(
                    url=stream_url,
                    width=self.canvas_width,
                    height=self.canvas_height,
                    fps=self.fps,
                    bitrate=self.bitrate
                )
                logger.info("StreamThread.run: C streamer initialized")
                self.status_signal.emit("Streaming started")
            except Exception as e:
                err_msg = str(e)
                logger.error("StreamThread.run: C extension init failed: %s", err_msg, exc_info=True)
                self.error_signal.emit(f"Stream init failed: {err_msg}")
                return

            frame_count = 0
            bytes_sent = 0
            
            try:
                while self.running and not self._stop_event.is_set():
                    if self.frame_data:
                        try:
                            success = streamer.send_frame(self.frame_data)
                            if not success:
                                err = streamer.get_error()
                                logger.error("StreamThread.run: send_frame failed: %s", err)
                                self.error_signal.emit(f"Frame send failed: {err}")
                                break
                            frame_count += 1
                            bytes_sent += len(self.frame_data)
                            if frame_count % 90 == 0:
                                logger.debug("StreamThread.run: sent %d frames (%.1f MB)", frame_count, bytes_sent / 1024 / 1024)
                        except Exception as e:
                            logger.error("StreamThread.run: send_frame exception: %s", e, exc_info=True)
                            break
                    time.sleep(1.0 / self.fps)
            finally:
                logger.info("StreamThread.run: flushing encoder (sent %d frames total)", frame_count)
                try:
                    streamer.flush()
                except Exception as e:
                    logger.error("StreamThread.run: flush error: %s", e)
                self.status_signal.emit("Stream stopped")
                
        elif self.encoder == "amf":
            cmd = [
                'ffmpeg', '-y',
                '-f', 'rawvideo', '-vcodec', 'rawvideo',
                '-s', f'{self.canvas_width}x{self.canvas_height}',
                '-pix_fmt', 'bgra', '-r', str(self.fps),
                '-i', '-',
                '-c:v', 'h264_amf', '-usage', 'ultralowlatency',
                '-b:v', f'{self.bitrate}k',
                '-maxrate', f'{self.bitrate}k',
                '-bufsize', f'{self.bitrate * 2}k',
                '-pix_fmt', 'yuv420p', '-g', str(self.fps * 2),
                '-f', 'flv', stream_url
            ]
            self._run_ffmpeg(cmd)
        else:
            cmd = [
                'ffmpeg', '-y',
                '-f', 'rawvideo', '-vcodec', 'rawvideo',
                '-s', f'{self.canvas_width}x{self.canvas_height}',
                '-pix_fmt', 'bgra', '-r', str(self.fps),
                '-i', '-',
                '-c:v', 'libx264', '-preset', 'veryfast',
                '-b:v', f'{self.bitrate}k',
                '-maxrate', f'{self.bitrate}k',
                '-bufsize', f'{self.bitrate * 2}k',
                '-pix_fmt', 'yuv420p', '-g', str(self.fps * 2),
                '-f', 'flv', stream_url
            ]
            self._run_ffmpeg(cmd)
            
        logger.info("StreamThread.run: thread finished")

    def _run_ffmpeg(self, cmd):
        logger.debug("StreamThread.run: ffmpeg cmd = %s", ' '.join(cmd[:15]) + ' ...')
        try:
            self.status_signal.emit("Starting stream...")
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            logger.info("StreamThread.run: ffmpeg process started (pid=%d)", process.pid)
            
            self.status_signal.emit("Streaming started")
            
            stderr_lines = []
            def read_stderr():
                while self.running and not self._stop_event.is_set():
                    line = process.stderr.readline()
                    if not line:
                        break
                    decoded = line.decode('utf-8', errors='replace').strip()
                    stderr_lines.append(decoded)
                    self.log_signal.emit(f"[FFmpeg] {decoded}")
                    logger.debug("FFmpeg stderr: %s", decoded)
            
            stderr_thread = threading.Thread(target=read_stderr, daemon=True)
            stderr_thread.start()
            
            frame_count = 0
            bytes_sent = 0
            
            while self.running and not self._stop_event.is_set():
                if self.frame_data:
                    try:
                        process.stdin.write(self.frame_data)
                        frame_count += 1
                        bytes_sent += len(self.frame_data)
                        if frame_count % 90 == 0:
                            logger.debug("StreamThread.run: sent %d frames (%.1f MB)", frame_count, bytes_sent / 1024 / 1024)
                    except BrokenPipeError as e:
                        logger.error("StreamThread.run: BrokenPipeError - stream connection lost (sent %d frames)", frame_count)
                        logger.error("StreamThread.run: last 20 FFmpeg stderr lines:")
                        for line in stderr_lines[-20:]:
                            logger.error("  %s", line)
                        self.error_signal.emit(f"Stream connection lost after {frame_count} frames. Check FFmpeg logs below.")
                        break
                    except Exception as e:
                        logger.error("StreamThread.run: write error: %s", e)
                        break
                time.sleep(1.0 / self.fps)
            
            logger.info("StreamThread.run: exiting loop, closing stdin (sent %d frames total)", frame_count)
            process.stdin.close()
            returncode = process.wait()
            logger.info("StreamThread.run: ffmpeg exited with code %d", returncode)
            
            if not self._stop_event.is_set():
                self.status_signal.emit("Stream stopped")
                
        except FileNotFoundError:
            logger.error("StreamThread.run: FFmpeg not found")
            self.error_signal.emit("FFmpeg not found. Please install FFmpeg.")
        except Exception as e:
            logger.error("StreamThread.run: unexpected error: %s", e, exc_info=True)
            self.error_signal.emit(f"Stream error: {str(e)}")
            
    def stop(self):
        logger.info("StreamThread.stop: signaling stop")
        self.running = False
        self._stop_event.set()


class DraggableImage:
    def __init__(self, pixmap, x, y, scale=1.0):
        self.original_pixmap = pixmap
        self.x = x
        self.y = y
        self.scale = scale
        self.selected = False
        self.dragging = False
        self.resizing = False
        self.resize_handle_size = 15
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        logger.debug("DraggableImage.__init__: pos=(%d,%d) scale=%.2f size=%dx%d",
                     x, y, scale, pixmap.width(), pixmap.height())
        
    def get_scaled_size(self):
        width = int(self.original_pixmap.width() * self.scale)
        height = int(self.original_pixmap.height() * self.scale)
        return width, height
        
    def get_rect(self):
        width, height = self.get_scaled_size()
        return QRect(self.x, self.y, width, height)
        
    def get_resize_handle_rect(self):
        rect = self.get_rect()
        return QRect(
            rect.right() - self.resize_handle_size,
            rect.bottom() - self.resize_handle_size,
            self.resize_handle_size,
            self.resize_handle_size
        )
        
    def contains(self, pos):
        return self.get_rect().contains(pos)
        
    def contains_resize_handle(self, pos):
        return self.get_resize_handle_rect().contains(pos)
        
    def draw(self, painter):
        width, height = self.get_scaled_size()
        scaled = self.original_pixmap.scaled(
            width, height,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation
        )
        painter.drawPixmap(self.x, self.y, scaled)
        
        if self.selected:
            painter.setPen(QPen(QColor(0, 120, 215), 2, Qt.DashLine))
            painter.drawRect(self.get_rect())
            handle_rect = self.get_resize_handle_rect()
            painter.setBrush(QBrush(QColor(0, 120, 215)))
            painter.drawRect(handle_rect)


class DraggableText:
    def __init__(self, text, x, y, font_family="Arial", font_size=48, color=QColor(255, 255, 255)):
        self.text = text
        self.x = x
        self.y = y
        self.font_family = font_family
        self.font_size = font_size
        self.color = color
        self.selected = False
        self.dragging = False
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        logger.debug("DraggableText.__init__: text='%s' pos=(%d,%d) font=%s size=%d",
                     text, x, y, font_family, font_size)
        
    def get_rect(self, painter):
        font = QFont(self.font_family, self.font_size)
        font.setBold(True)
        metrics = QFontMetrics(font)
        rect = metrics.boundingRect(self.text)
        return QRect(self.x, self.y - rect.height(), rect.width(), rect.height())
        
    def contains(self, pos, painter):
        rect = self.get_rect(painter)
        return rect.contains(pos)
        
    def draw(self, painter):
        font = QFont(self.font_family, self.font_size)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self.color)
        painter.drawText(self.x, self.y, self.text)
        
        if self.selected:
            rect = self.get_rect(painter)
            painter.setPen(QPen(QColor(0, 120, 215), 2, Qt.DashLine))
            painter.drawRect(rect)


class DrawingCanvas(QWidget):
    frame_ready = pyqtSignal(bytes)
    
    def __init__(self, width=1920, height=1080):
        super().__init__()
        self.native_width = width
        self.native_height = height
        logger.info("DrawingCanvas.__init__: native resolution %dx%d", width, height)
        
        self.setMinimumSize(640, 360)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        
        self.bg_color = QColor(30, 30, 30)
        self.bg_image = None
        self.bg_image_path = None
        self.drawing = False
        self.brush_color = QColor(255, 255, 255)
        self.brush_size = 5
        self.eraser = False
        
        self.last_point = QPoint()
        self.image = QImage(self.native_width, self.native_height, QImage.Format_ARGB32)
        self.image.fill(self.bg_color.rgb())
        
        self.images = []
        self.texts = []
        self.selected_item = None
        self.scale_factor = 1.0
        
    def get_display_scale(self):
        avail_width = self.width()
        avail_height = self.height()
        scale_w = avail_width / self.native_width
        scale_h = avail_height / self.native_height
        return min(scale_w, scale_h)
        
    def display_to_native(self, display_pos):
        scale = self.get_display_scale()
        offset_x = (self.width() - self.native_width * scale) / 2
        offset_y = (self.height() - self.native_height * scale) / 2
        native_x = int((display_pos.x() - offset_x) / scale)
        native_y = int((display_pos.y() - offset_y) / scale)
        return QPoint(native_x, native_y)
        
    def clear_canvas(self):
        logger.debug("DrawingCanvas.clear_canvas")
        self.image.fill(self.bg_color.rgb())
        self.update()

    def set_background_image(self, file_path):
        if file_path and os.path.exists(file_path):
            logger.info("DrawingCanvas.set_background_image: %s", file_path)
            pm = QPixmap(file_path)
            if not pm.isNull():
                scaled = pm.scaled(self.native_width, self.native_height,
                                   Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                self.bg_image = scaled.toImage().convertToFormat(QImage.Format_ARGB32)
                self.bg_image_path = file_path
                self.update()
        else:
            self.bg_image = None
            self.bg_image_path = None
            self.update()

    def remove_background_image(self):
        logger.debug("DrawingCanvas.remove_background_image")
        self.bg_image = None
        self.bg_image_path = None
        self.update()
        
    def get_frame_bytes(self):
        if self.bg_image:
            temp_image = QImage(self.bg_image)
        else:
            temp_image = QImage(self.image)
        painter = QPainter(temp_image)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        if not self.bg_image:
            painter.drawImage(0, 0, self.image)

        for img in self.images:
            img.draw(painter)
        for txt in self.texts:
            txt.draw(painter)

        painter.end()

        bits = temp_image.bits()
        bits.setsize(temp_image.byteCount())
        return bytes(bits)
        
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        
        painter.fillRect(self.rect(), QColor(20, 20, 30))
        
        scale = self.get_display_scale()
        self.scale_factor = scale
        
        offset_x = (self.width() - self.native_width * scale) / 2
        offset_y = (self.height() - self.native_height * scale) / 2
        
        painter.translate(offset_x, offset_y)
        painter.scale(scale, scale)

        if self.bg_image:
            painter.drawImage(0, 0, self.bg_image)
        else:
            painter.drawImage(0, 0, self.image)

        for img in self.images:
            img.draw(painter)
        for txt in self.texts:
            txt.draw(painter)
            
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            native_pos = self.display_to_native(event.pos())
            logger.debug("DrawingCanvas.mousePressEvent: display=%s native=%s", event.pos(), native_pos)
            
            for txt in reversed(self.texts):
                temp_painter = QPainter(self)
                if txt.contains(native_pos, temp_painter):
                    temp_painter.end()
                    self.selected_item = txt
                    txt.selected = True
                    txt.dragging = True
                    txt.drag_offset_x = native_pos.x() - txt.x
                    txt.drag_offset_y = native_pos.y() - txt.y
                    for other in self.texts + self.images:
                        if other != txt:
                            other.selected = False
                    logger.debug("DrawingCanvas.mousePressEvent: selected text '%s'", txt.text)
                    self.update()
                    return
                temp_painter.end()
            
            for img in reversed(self.images):
                if img.contains_resize_handle(native_pos):
                    self.selected_item = img
                    img.selected = True
                    img.resizing = True
                    img.drag_offset_x = native_pos.x()
                    img.drag_offset_y = native_pos.y()
                    for other in self.texts + self.images:
                        if other != img:
                            other.selected = False
                    logger.debug("DrawingCanvas.mousePressEvent: resize handle on image")
                    self.update()
                    return
                elif img.contains(native_pos):
                    self.selected_item = img
                    img.selected = True
                    img.dragging = True
                    img.drag_offset_x = native_pos.x() - img.x
                    img.drag_offset_y = native_pos.y() - img.y
                    for other in self.texts + self.images:
                        if other != img:
                            other.selected = False
                    logger.debug("DrawingCanvas.mousePressEvent: selected image at (%d,%d)", img.x, img.y)
                    self.update()
                    return
                    
            if self.selected_item:
                self.selected_item.selected = False
                self.selected_item = None
                logger.debug("DrawingCanvas.mousePressEvent: deselected item")
                self.update()
                
            self.drawing = True
            self.last_point = native_pos
            logger.debug("DrawingCanvas.mousePressEvent: drawing mode started")
                
    def mouseMoveEvent(self, event):
        native_pos = self.display_to_native(event.pos())
        
        if self.selected_item and self.selected_item.resizing:
            dx = native_pos.x() - self.selected_item.drag_offset_x
            dy = native_pos.y() - self.selected_item.drag_offset_y
            new_scale = max(0.1, self.selected_item.scale + (dx + dy) / 200.0)
            self.selected_item.scale = new_scale
            self.selected_item.drag_offset_x = native_pos.x()
            self.selected_item.drag_offset_y = native_pos.y()
            self.update()
            return
            
        if self.selected_item and self.selected_item.dragging:
            self.selected_item.x = native_pos.x() - self.selected_item.drag_offset_x
            self.selected_item.y = native_pos.y() - self.selected_item.drag_offset_y
            self.update()
            return
            
        if self.drawing:
            painter = QPainter(self.image)
            painter.setRenderHint(QPainter.Antialiasing)
            
            if self.eraser:
                pen = QPen(self.bg_color, self.brush_size * 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            else:
                pen = QPen(self.brush_color, self.brush_size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
                
            painter.setPen(pen)
            painter.drawLine(self.last_point, native_pos)
            painter.end()
            
            self.last_point = native_pos
            self.update()
            
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            logger.debug("DrawingCanvas.mouseReleaseEvent: drawing=%s selected=%s", self.drawing, self.selected_item)
            self.drawing = False
            if self.selected_item:
                self.selected_item.dragging = False
                self.selected_item.resizing = False
                
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            logger.debug("DrawingCanvas.dragEnterEvent: accepting drop")
            event.acceptProposedAction()
            
    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                file_path = url.toLocalFile()
                if file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')):
                    logger.info("DrawingCanvas.dropEvent: dropping image %s", file_path)
                    self.add_image(file_path)
                    
    def add_image(self, file_path):
        logger.debug("DrawingCanvas.add_image: %s", file_path)
        pixmap = QPixmap(file_path)
        if not pixmap.isNull():
            max_width = self.native_width // 3
            if pixmap.width() > max_width:
                pixmap = pixmap.scaledToWidth(max_width, Qt.SmoothTransformation)
                
            x = (self.native_width - pixmap.width()) // 2
            y = (self.native_height - pixmap.height()) // 2
            
            img = DraggableImage(pixmap, x, y)
            self.images.append(img)
            self.update()
            logger.info("DrawingCanvas.add_image: added image at (%d,%d) size=%dx%d", x, y, pixmap.width(), pixmap.height())
            
    def add_text(self, text, font_family="Arial", font_size=48, color=QColor(255, 255, 255)):
        x = self.native_width // 4
        y = self.native_height // 2
        txt = DraggableText(text, x, y, font_family, font_size, color)
        self.texts.append(txt)
        self.update()
        logger.info("DrawingCanvas.add_text: added '%s' at (%d,%d)", text, x, y)


class SettingsPanel(QWidget):
    stream_start_requested = pyqtSignal(dict)
    stream_stop_requested = pyqtSignal()
    clear_canvas_requested = pyqtSignal()
    import_image_requested = pyqtSignal()
    add_text_requested = pyqtSignal()
    choose_bg_color_requested = pyqtSignal()
    choose_brush_color_requested = pyqtSignal()
    set_bg_image_requested = pyqtSignal()
    remove_bg_image_requested = pyqtSignal()
    
    def __init__(self):
        super().__init__()
        self.setup_ui()
        
    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(12)
        
        scroll_layout.addWidget(self.create_canvas_group())
        scroll_layout.addWidget(self.create_drawing_tools_group())
        scroll_layout.addWidget(self.create_media_group())
        scroll_layout.addWidget(self.create_stream_settings_group())
        scroll_layout.addWidget(self.create_stream_status_group())
        scroll_layout.addStretch()
        
        scroll.setWidget(scroll_widget)
        main_layout.addWidget(scroll)
        
    def create_canvas_group(self):
        group = QGroupBox("Canvas")
        layout = QVBoxLayout(group)

        res_layout = QHBoxLayout()
        res_label = QLabel("Resolution:")
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(["1920x1080 (1080p)", "1280x720 (720p)"])
        res_layout.addWidget(res_label)
        res_layout.addWidget(self.resolution_combo)
        layout.addLayout(res_layout)

        bg_layout = QHBoxLayout()
        bg_label = QLabel("Background:")
        self.bg_color_btn = QPushButton()
        self.bg_color_btn.setFixedSize(40, 30)
        self.bg_color_btn.setStyleSheet("background-color: rgb(30, 30, 30); border: 1px solid #ccc;")
        self.bg_color_btn.clicked.connect(self.choose_bg_color_requested.emit)
        bg_layout.addWidget(bg_label)
        bg_layout.addWidget(self.bg_color_btn)
        bg_layout.addStretch()
        layout.addLayout(bg_layout)

        bg_img_layout = QVBoxLayout()
        bg_img_label = QLabel("Background Image:")
        bg_img_layout.addWidget(bg_img_label)

        bg_img_btn_layout = QHBoxLayout()
        self.set_bg_image_btn = QPushButton("Set Image")
        self.set_bg_image_btn.clicked.connect(self.set_bg_image_requested.emit)
        self.remove_bg_image_btn = QPushButton("Remove")
        self.remove_bg_image_btn.clicked.connect(self.remove_bg_image_requested.emit)
        bg_img_btn_layout.addWidget(self.set_bg_image_btn)
        bg_img_btn_layout.addWidget(self.remove_bg_image_btn)
        bg_img_layout.addLayout(bg_img_btn_layout)

        self.bg_image_label = QLabel("No image set")
        self.bg_image_label.setStyleSheet("color: #6c7086; font-size: 11px;")
        self.bg_image_label.setWordWrap(True)
        bg_img_layout.addWidget(self.bg_image_label)

        layout.addLayout(bg_img_layout)

        return group
        
    def create_drawing_tools_group(self):
        group = QGroupBox("Drawing Tools")
        layout = QVBoxLayout(group)
        
        color_layout = QHBoxLayout()
        color_label = QLabel("Brush Color:")
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(40, 30)
        self.color_btn.setStyleSheet("background-color: white; border: 1px solid #ccc;")
        self.color_btn.clicked.connect(self.choose_brush_color_requested.emit)
        color_layout.addWidget(color_label)
        color_layout.addWidget(self.color_btn)
        color_layout.addStretch()
        layout.addLayout(color_layout)
        
        brush_size_layout = QHBoxLayout()
        brush_size_label = QLabel("Brush Size:")
        self.brush_size_slider = QSlider(Qt.Horizontal)
        self.brush_size_slider.setMinimum(1)
        self.brush_size_slider.setMaximum(50)
        self.brush_size_slider.setValue(5)
        self.brush_size_value_label = QLabel("5")
        self.brush_size_slider.valueChanged.connect(
            lambda v: self.brush_size_value_label.setText(str(v))
        )
        brush_size_layout.addWidget(brush_size_label)
        brush_size_layout.addWidget(self.brush_size_slider)
        brush_size_layout.addWidget(self.brush_size_value_label)
        layout.addLayout(brush_size_layout)
        
        self.eraser_btn = QPushButton("Eraser")
        self.eraser_btn.setCheckable(True)
        layout.addWidget(self.eraser_btn)
        
        clear_btn = QPushButton("Clear Canvas")
        clear_btn.clicked.connect(self.clear_canvas_requested.emit)
        layout.addWidget(clear_btn)
        
        return group
        
    def create_media_group(self):
        group = QGroupBox("Media")
        layout = QVBoxLayout(group)
        
        import_btn = QPushButton("Import Image")
        import_btn.clicked.connect(self.import_image_requested.emit)
        layout.addWidget(import_btn)
        
        add_text_btn = QPushButton("Add Text")
        add_text_btn.clicked.connect(self.add_text_requested.emit)
        layout.addWidget(add_text_btn)
        
        return group
        
    def create_stream_settings_group(self):
        group = QGroupBox("Stream Settings")
        layout = QVBoxLayout(group)
        
        encoder_layout = QHBoxLayout()
        encoder_label = QLabel("Encoder:")
        self.encoder_combo = QComboBox()
        self.encoder_combo.addItems(["VAAPI (AMD GPU)", "AMF (AMD GPU)", "x264 (CPU)"])
        self.encoder_combo.currentTextChanged.connect(self.update_encoder_settings)
        encoder_layout.addWidget(encoder_label)
        encoder_layout.addWidget(self.encoder_combo)
        layout.addLayout(encoder_layout)
        
        platform_layout = QHBoxLayout()
        platform_label = QLabel("Platform:")
        self.platform_combo = QComboBox()
        self.platform_combo.addItems(["Twitch", "Kick", "YouTube", "Facebook", "Custom"])
        self.platform_combo.currentTextChanged.connect(self.update_rtmp_url)
        platform_layout.addWidget(platform_label)
        platform_layout.addWidget(self.platform_combo)
        layout.addLayout(platform_layout)
        
        rtmp_layout = QVBoxLayout()
        rtmp_label = QLabel("RTMP URL:")
        self.rtmp_url_input = QLineEdit("rtmp://live.twitch.tv/app")
        self.rtmp_url_input.setPlaceholderText("rtmp://...")
        rtmp_layout.addWidget(rtmp_label)
        rtmp_layout.addWidget(self.rtmp_url_input)
        layout.addLayout(rtmp_layout)
        
        key_layout = QVBoxLayout()
        key_label = QLabel("Stream Key:")
        self.stream_key_input = QLineEdit()
        self.stream_key_input.setEchoMode(QLineEdit.Password)
        self.stream_key_input.setPlaceholderText("Enter your stream key")
        key_layout.addWidget(key_label)
        key_layout.addWidget(self.stream_key_input)
        layout.addLayout(key_layout)
        
        bitrate_layout = QHBoxLayout()
        bitrate_label = QLabel("Bitrate (kbps):")
        self.bitrate_spinbox = QSpinBox()
        self.bitrate_spinbox.setMinimum(1000)
        self.bitrate_spinbox.setMaximum(8000)
        self.bitrate_spinbox.setValue(4000)
        self.bitrate_spinbox.setSingleStep(500)
        bitrate_layout.addWidget(bitrate_label)
        bitrate_layout.addWidget(self.bitrate_spinbox)
        layout.addLayout(bitrate_layout)
        
        fps_layout = QHBoxLayout()
        fps_label = QLabel("FPS:")
        self.fps_combo = QComboBox()
        self.fps_combo.addItems(["24", "30", "60"])
        self.fps_combo.setCurrentText("30")
        fps_layout.addWidget(fps_label)
        fps_layout.addWidget(self.fps_combo)
        layout.addLayout(fps_layout)
        
        self.show_key_btn = QPushButton("Show Stream Key")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.toggled.connect(
            lambda checked: self.stream_key_input.setEchoMode(
                QLineEdit.Normal if checked else QLineEdit.Password
            )
        )
        layout.addWidget(self.show_key_btn)
        
        return group
        
    def create_stream_status_group(self):
        group = QGroupBox("Stream Control")
        layout = QVBoxLayout(group)
        
        self.stream_btn = QPushButton("Start Stream")
        self.stream_btn.setStyleSheet(
            "QPushButton { background-color: #e74c3c; color: white; padding: 10px; font-weight: bold; }"
            "QPushButton:hover { background-color: #c0392b; }"
        )
        self.stream_btn.clicked.connect(self.toggle_stream)
        layout.addWidget(self.stream_btn)
        
        status_layout = QHBoxLayout()
        status_label = QLabel("Status:")
        self.status_label = QLabel("Offline")
        self.status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
        status_layout.addWidget(status_label)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)
        
        self.duration_label = QLabel("Duration: 00:00:00")
        layout.addWidget(self.duration_label)
        
        return group
        
    def update_encoder_settings(self, encoder):
        logger.debug("SettingsPanel.update_encoder_settings: encoder=%s", encoder)
        if "VAAPI" in encoder:
            self.bitrate_spinbox.setMaximum(12000)
        elif "AMF" in encoder:
            self.bitrate_spinbox.setMaximum(12000)
        else:
            self.bitrate_spinbox.setMaximum(8000)
        
    def update_rtmp_url(self, platform):
        logger.debug("SettingsPanel.update_rtmp_url: platform=%s", platform)
        urls = {
            "Twitch": "rtmp://live.twitch.tv/app",
            "Kick": "rtmp://stream.kick.com/live",
            "YouTube": "rtmp://a.rtmp.youtube.com/live2",
            "Facebook": "rtmps://live-api-s.facebook.com:443/rtmp/",
            "Custom": ""
        }
        self.rtmp_url_input.setText(urls.get(platform, ""))
        
    def toggle_stream(self):
        logger.debug("SettingsPanel.toggle_stream: current=%s", self.stream_btn.text())
        if self.stream_btn.text() == "Start Stream":
            if not self.stream_key_input.text():
                logger.warning("SettingsPanel.toggle_stream: missing stream key")
                QMessageBox.warning(self, "Missing Stream Key", "Please enter your stream key.")
                return
                
            encoder_map = {
                "VAAPI (AMD GPU)": "vaapi",
                "AMF (AMD GPU)": "amf",
                "x264 (CPU)": "x264"
            }
            
            settings = {
                'rtmp_url': self.rtmp_url_input.text(),
                'stream_key': self.stream_key_input.text(),
                'bitrate': self.bitrate_spinbox.value(),
                'fps': int(self.fps_combo.currentText()),
                'encoder': encoder_map.get(self.encoder_combo.currentText(), "vaapi")
            }
            logger.info("SettingsPanel.toggle_stream: emitting start with encoder=%s bitrate=%d fps=%d",
                        settings['encoder'], settings['bitrate'], settings['fps'])
            self.stream_start_requested.emit(settings)
        else:
            logger.info("SettingsPanel.toggle_stream: emitting stop")
            self.stream_stop_requested.emit()
            
    def set_streaming(self, streaming):
        logger.debug("SettingsPanel.set_streaming: streaming=%s", streaming)
        if streaming:
            self.stream_btn.setText("Stop Stream")
            self.stream_btn.setStyleSheet(
                "QPushButton { background-color: #2ecc71; color: white; padding: 10px; font-weight: bold; }"
                "QPushButton:hover { background-color: #27ae60; }"
            )
            self.status_label.setText("LIVE")
            self.status_label.setStyleSheet("color: #2ecc71; font-weight: bold;")
        else:
            self.stream_btn.setText("Start Stream")
            self.stream_btn.setStyleSheet(
                "QPushButton { background-color: #e74c3c; color: white; padding: 10px; font-weight: bold; }"
                "QPushButton:hover { background-color: #c0392b; }"
            )
            self.status_label.setText("Offline")
            self.status_label.setStyleSheet("color: #e74c3c; font-weight: bold;")
            self.duration_label.setText("Duration: 00:00:00")


class TextDialog(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Dialog)
        self.setWindowTitle("Add Text")
        self.setFixedSize(350, 250)
        
        layout = QVBoxLayout(self)
        
        self.text_input = QLineEdit()
        self.text_input.setPlaceholderText("Enter text...")
        layout.addWidget(QLabel("Text:"))
        layout.addWidget(self.text_input)
        
        font_layout = QHBoxLayout()
        self.font_combo = QComboBox()
        self.font_combo.addItems(["Arial", "Courier New", "Georgia", "Times New Roman", "Verdana"])
        self.font_size_spinbox = QSpinBox()
        self.font_size_spinbox.setMinimum(12)
        self.font_size_spinbox.setMaximum(200)
        self.font_size_spinbox.setValue(48)
        font_layout.addWidget(QLabel("Font:"))
        font_layout.addWidget(self.font_combo)
        font_layout.addWidget(QLabel("Size:"))
        font_layout.addWidget(self.font_size_spinbox)
        layout.addLayout(font_layout)
        
        color_layout = QHBoxLayout()
        self.color_btn = QPushButton("Choose Color")
        self.color_btn.clicked.connect(self.choose_color)
        self.selected_color = QColor(255, 255, 255)
        self.color_preview = QLabel()
        self.color_preview.setFixedSize(30, 30)
        self.color_preview.setStyleSheet("background-color: white; border: 1px solid #ccc;")
        color_layout.addWidget(self.color_btn)
        color_layout.addWidget(self.color_preview)
        color_layout.addStretch()
        layout.addLayout(color_layout)
        
        btn_layout = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.close)
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self.accept_text)
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.add_btn)
        layout.addLayout(btn_layout)
        
    def choose_color(self):
        color = QColorDialog.getColor(self.selected_color, self, "Choose Text Color")
        if color.isValid():
            self.selected_color = color
            self.color_preview.setStyleSheet(
                f"background-color: rgb({color.red()}, {color.green()}, {color.blue()}); border: 1px solid #ccc;"
            )
            
    def accept_text(self):
        text = self.text_input.text().strip()
        if text:
            self.result = {
                'text': text,
                'font_family': self.font_combo.currentText(),
                'font_size': self.font_size_spinbox.value(),
                'color': self.selected_color
            }
            self.accept()
        else:
            QMessageBox.warning(self, "Empty Text", "Please enter some text.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        logger.info("=" * 60)
        logger.info("MainWindow.__init__: starting OmniStream Studio")
        logger.info("=" * 60)
        
        self.config = ConfigManager()
        self.canvas_width = 1920
        self.canvas_height = 1080
        self.stream_thread = None
        self.stream_start_time = None
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_stream_frame)
        self.duration_timer = QTimer()
        self.duration_timer.timeout.connect(self.update_duration)
        
        self.setup_ui()
        self.load_config()
        self.connect_signals()
        
    def setup_ui(self):
        logger.debug("MainWindow.setup_ui")
        self.setWindowTitle("OmniStream Studio")
        self.setMinimumSize(1200, 800)
        self.resize(1400, 900)
        
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)
        
        left_split = QVBoxLayout()
        
        canvas_container = QWidget()
        canvas_container.setStyleSheet("background-color: #141420;")
        canvas_layout = QVBoxLayout(canvas_container)
        canvas_layout.setContentsMargins(10, 10, 10, 10)
        
        self.canvas = DrawingCanvas(self.canvas_width, self.canvas_height)
        canvas_layout.addWidget(self.canvas)
        
        left_split.addWidget(canvas_container, stretch=3)
        
        self.log_tabs = QTabWidget()
        self.log_tabs.setMaximumHeight(200)
        
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("background-color: #1a1a2e; color: #cdd6f4; font-family: monospace; font-size: 11px;")
        self.log_tabs.addTab(self.log_text, "App Log")
        
        self.ffmpeg_log_text = QTextEdit()
        self.ffmpeg_log_text.setReadOnly(True)
        self.ffmpeg_log_text.setStyleSheet("background-color: #1a1a2e; color: #f38ba8; font-family: monospace; font-size: 11px;")
        self.log_tabs.addTab(self.ffmpeg_log_text, "FFmpeg Log")
        
        left_split.addWidget(self.log_tabs, stretch=1)
        
        main_layout.addLayout(left_split, 3)
        
        self.settings_panel = SettingsPanel()
        self.settings_panel.setMinimumWidth(300)
        self.settings_panel.setMaximumWidth(380)
        main_layout.addWidget(self.settings_panel, stretch=1)
        
        log_handler = LogHandler(self.log_text)
        log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
        logging.getLogger().addHandler(log_handler)
        
        logger.debug("MainWindow.setup_ui: UI built")
        
    def connect_signals(self):
        logger.debug("MainWindow.connect_signals")
        self.settings_panel.stream_start_requested.connect(self.start_stream)
        self.settings_panel.stream_stop_requested.connect(self.stop_stream)
        self.settings_panel.clear_canvas_requested.connect(self.canvas.clear_canvas)
        self.settings_panel.import_image_requested.connect(self.import_image)
        self.settings_panel.add_text_requested.connect(self.show_text_dialog)
        self.settings_panel.brush_size_slider.valueChanged.connect(
            lambda v: setattr(self.canvas, 'brush_size', v) or self.config.set("brush_size", v)
        )
        self.settings_panel.eraser_btn.toggled.connect(
            lambda checked: setattr(self.canvas, 'eraser', checked)
        )
        self.settings_panel.resolution_combo.currentTextChanged.connect(self.change_resolution)
        self.settings_panel.choose_bg_color_requested.connect(self.change_bg_color)
        self.settings_panel.choose_brush_color_requested.connect(self.change_brush_color)
        self.settings_panel.encoder_combo.currentTextChanged.connect(
            lambda t: self.config.set("encoder", t)
        )
        self.settings_panel.platform_combo.currentTextChanged.connect(
            lambda t: self.config.set("platform", t)
        )
        self.settings_panel.rtmp_url_input.textChanged.connect(
            lambda t: self.config.set("rtmp_url", t)
        )
        self.settings_panel.stream_key_input.textChanged.connect(
            lambda t: self.config.set("stream_key", t)
        )
        self.settings_panel.bitrate_spinbox.valueChanged.connect(
            lambda v: self.config.set("bitrate", v)
        )
        self.settings_panel.fps_combo.currentTextChanged.connect(
            lambda t: self.config.set("fps", t)
        )
        self.settings_panel.set_bg_image_requested.connect(self.set_background_image)
        self.settings_panel.remove_bg_image_requested.connect(self.remove_background_image)
        
    def load_config(self):
        logger.debug("MainWindow.load_config")
        res = self.config.get("resolution", "1920x1080 (1080p)")
        idx = self.settings_panel.resolution_combo.findText(res)
        if idx >= 0:
            self.settings_panel.resolution_combo.setCurrentIndex(idx)
        self.change_resolution(res)
        
        bg = self.config.get("bg_color", [30, 30, 30])
        self.canvas.bg_color = QColor(*bg)
        self.settings_panel.bg_color_btn.setStyleSheet(
            f"background-color: rgb({bg[0]}, {bg[1]}, {bg[2]}); border: 1px solid #ccc;"
        )
        
        bc = self.config.get("brush_color", [255, 255, 255])
        self.canvas.brush_color = QColor(*bc)
        self.settings_panel.color_btn.setStyleSheet(
            f"background-color: rgb({bc[0]}, {bc[1]}, {bc[2]}); border: 1px solid #ccc;"
        )
        
        bs = self.config.get("brush_size", 5)
        self.canvas.brush_size = bs
        self.settings_panel.brush_size_slider.setValue(bs)
        
        enc = self.config.get("encoder", "VAAPI (AMD GPU)")
        idx = self.settings_panel.encoder_combo.findText(enc)
        if idx >= 0:
            self.settings_panel.encoder_combo.setCurrentIndex(idx)
        
        plat = self.config.get("platform", "Twitch")
        idx = self.settings_panel.platform_combo.findText(plat)
        if idx >= 0:
            self.settings_panel.platform_combo.setCurrentIndex(idx)
        
        self.settings_panel.rtmp_url_input.setText(self.config.get("rtmp_url", "rtmp://live.twitch.tv/app"))
        self.settings_panel.stream_key_input.setText(self.config.get("stream_key", ""))
        self.settings_panel.bitrate_spinbox.setValue(self.config.get("bitrate", 4000))
        
        fps = self.config.get("fps", "30")
        idx = self.settings_panel.fps_combo.findText(fps)
        if idx >= 0:
            self.settings_panel.fps_combo.setCurrentIndex(idx)

        bg_img_path = self.config.get("bg_image_path", "")
        if bg_img_path and os.path.exists(bg_img_path):
            self.canvas.set_background_image(bg_img_path)
            self.settings_panel.bg_image_label.setText(os.path.basename(bg_img_path))

        logger.info("MainWindow.load_config: loaded resolution=%s encoder=%s platform=%s bitrate=%d fps=%s",
                    res, enc, plat, self.config.get("bitrate", 4000), fps)
        
    def change_resolution(self, text):
        logger.info("MainWindow.change_resolution: %s", text)
        self.config.set("resolution", text)
        if "1920" in text:
            self.canvas_width = 1920
            self.canvas_height = 1080
        else:
            self.canvas_width = 1280
            self.canvas_height = 720

        old_image = self.canvas.image
        self.canvas.native_width = self.canvas_width
        self.canvas.native_height = self.canvas_height
        self.canvas.image = QImage(self.canvas_width, self.canvas_height, QImage.Format_ARGB32)
        self.canvas.image.fill(self.canvas.bg_color.rgb())

        painter = QPainter(self.canvas.image)
        painter.drawImage(0, 0, old_image)
        painter.end()

        if self.canvas.bg_image_path and os.path.exists(self.canvas.bg_image_path):
            self.canvas.set_background_image(self.canvas.bg_image_path)

        self.canvas.update()
        logger.info("MainWindow.change_resolution: canvas now %dx%d", self.canvas_width, self.canvas_height)
        
    def change_bg_color(self):
        color = QColorDialog.getColor(self.canvas.bg_color, self, "Choose Background Color")
        if color.isValid():
            logger.info("MainWindow.change_bg_color: rgb(%d,%d,%d)", color.red(), color.green(), color.blue())
            self.canvas.bg_color = color
            self.settings_panel.bg_color_btn.setStyleSheet(
                f"background-color: rgb({color.red()}, {color.green()}, {color.blue()}); border: 1px solid #ccc;"
            )
            self.config.set("bg_color", [color.red(), color.green(), color.blue()])
            self.canvas.clear_canvas()
            
    def change_brush_color(self):
        color = QColorDialog.getColor(self.canvas.brush_color, self, "Choose Brush Color")
        if color.isValid():
            logger.info("MainWindow.change_brush_color: rgb(%d,%d,%d)", color.red(), color.green(), color.blue())
            self.canvas.brush_color = color
            self.settings_panel.color_btn.setStyleSheet(
                f"background-color: rgb({color.red()}, {color.green()}, {color.blue()}); border: 1px solid #ccc;"
            )
            self.config.set("brush_color", [color.red(), color.green(), color.blue()])
            
    def import_image(self):
        logger.debug("MainWindow.import_image: opening file dialog")
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Image",
            "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"
        )
        if file_path:
            logger.info("MainWindow.import_image: selected %s", file_path)
            self.canvas.add_image(file_path)
        else:
            logger.debug("MainWindow.import_image: cancelled")

    def set_background_image(self):
        logger.debug("MainWindow.set_background_image: opening file dialog")
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Set Background Image",
            "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)"
        )
        if file_path:
            logger.info("MainWindow.set_background_image: selected %s", file_path)
            self.canvas.set_background_image(file_path)
            self.config.set("bg_image_path", file_path)
            self.settings_panel.bg_image_label.setText(os.path.basename(file_path))
        else:
            logger.debug("MainWindow.set_background_image: cancelled")

    def remove_background_image(self):
        logger.debug("MainWindow.remove_background_image")
        self.canvas.remove_background_image()
        self.config.set("bg_image_path", "")
        self.settings_panel.bg_image_label.setText("No image set")
            
    def show_text_dialog(self):
        logger.debug("MainWindow.show_text_dialog")
        dialog = TextDialog(self)
        dialog.text_input.setFocus()
        if dialog.exec_():
            result = dialog.result
            logger.info("MainWindow.show_text_dialog: adding text '%s' font=%s size=%d",
                        result['text'], result['font_family'], result['font_size'])
            self.canvas.add_text(
                result['text'],
                result['font_family'],
                result['font_size'],
                result['color']
            )
        else:
            logger.debug("MainWindow.show_text_dialog: cancelled")
            
    def start_stream(self, settings):
        logger.info("MainWindow.start_stream: url=%s/*** bitrate=%d fps=%d encoder=%s",
                     settings['rtmp_url'], settings['bitrate'], settings['fps'], settings['encoder'])
        if self.stream_thread and self.stream_thread.isRunning():
            logger.warning("MainWindow.start_stream: already running, ignoring")
            return
            
        self.stream_thread = StreamThread(
            canvas_width=self.canvas_width,
            canvas_height=self.canvas_height,
            rtmp_url=settings['rtmp_url'],
            stream_key=settings['stream_key'],
            bitrate=settings['bitrate'],
            fps=settings['fps'],
            encoder=settings['encoder']
        )
        
        self.stream_thread.status_signal.connect(self.on_stream_status)
        self.stream_thread.error_signal.connect(self.on_stream_error)
        self.stream_thread.log_signal.connect(self.on_ffmpeg_log)
        
        self.stream_thread.start()
        self.settings_panel.set_streaming(True)
        
        self.stream_start_time = time.time()
        self.timer.start(1000 // settings['fps'])
        self.duration_timer.start(1000)
        
        logger.info("MainWindow.start_stream: stream thread started, timers active")
        
    def stop_stream(self):
        logger.info("MainWindow.stop_stream")
        if self.stream_thread:
            self.stream_thread.stop()
            self.stream_thread.wait()
            self.stream_thread = None
            
        self.timer.stop()
        self.duration_timer.stop()
        self.settings_panel.set_streaming(False)
        self.stream_start_time = None
        logger.info("MainWindow.stop_stream: thread joined, timers stopped")
        
    def update_stream_frame(self):
        if self.stream_thread and self.stream_thread.isRunning():
            frame_bytes = self.canvas.get_frame_bytes()
            self.stream_thread.set_frame(frame_bytes)
            
    def update_duration(self):
        if self.stream_start_time:
            elapsed = int(time.time() - self.stream_start_time)
            hours = elapsed // 3600
            minutes = (elapsed % 3600) // 60
            seconds = elapsed % 60
            self.settings_panel.duration_label.setText(
                f"Duration: {hours:02d}:{minutes:02d}:{seconds:02d}"
            )
            
    def on_stream_status(self, message):
        logger.info("MainWindow.on_stream_status: %s", message)
        if "error" in message.lower():
            self.on_stream_error(message)
            
    def on_stream_error(self, message):
        logger.error("MainWindow.on_stream_error: %s", message)
        QMessageBox.critical(self, "Stream Error", message)
        self.stop_stream()
        
    def on_ffmpeg_log(self, message):
        cursor = self.ffmpeg_log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(message + "\n")
        self.ffmpeg_log_text.setTextCursor(cursor)
        self.ffmpeg_log_text.ensureCursorVisible()
        
    def closeEvent(self, event):
        logger.info("MainWindow.closeEvent: shutting down")
        if self.stream_thread and self.stream_thread.isRunning():
            self.stop_stream()
        self.config.save()
        logger.info("MainWindow.closeEvent: done")
        event.accept()


def main():
    logger.info("main: application starting")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    qss_path = os.path.join(os.path.dirname(__file__), "style.qss")
    if os.path.exists(qss_path):
        with open(qss_path, "r") as f:
            app.setStyleSheet(f.read())
        logger.debug("main: stylesheet loaded from %s", qss_path)
    
    window = MainWindow()
    window.show()
    
    logger.info("main: main window shown, entering event loop")
    ret = app.exec_()
    logger.info("main: event loop exited with code %d", ret)
    sys.exit(ret)


if __name__ == "__main__":
    main()
