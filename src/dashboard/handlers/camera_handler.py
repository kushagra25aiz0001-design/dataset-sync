"""
Camera Handler — Video Capture & Recording
===========================================
Manages the camera capture loop, MJPEG preview generation, and
video/frame recording with a dedicated writer thread for disk I/O.

Supports:
    - HDMI capture cards (Sony A6700, Elgato, etc.)
    - Standard USB webcams
    - V4L2 device enumeration via v4l2-ctl
    - Hot-switching between cameras
    - Dual-path: preview (scaled JPEG) + recording (full resolution)
"""

import csv
import glob
import os
import queue
import re
import subprocess
import threading
import time

os.environ.setdefault('OPENCV_LOG_LEVEL', 'SILENT')
import cv2

from src.recorder.sensor_registry import SensorRegistry, SensorState


class CameraHandler:
    """
    Encapsulates camera capture, preview, and recording.

    The capture loop runs in its own thread, reading frames from
    the V4L2 device. A separate writer thread handles all disk I/O
    (video encoding or JPEG writes), decoupled via a bounded queue.
    """

    def __init__(self, registry: SensorRegistry, sio,
                 source='auto', resolution=(1920, 1080),
                 record_format='video'):
        self.registry = registry
        self.sio = sio
        self.source = source
        self.resolution = resolution
        self.record_format = record_format

        # Camera info (populated when camera connects)
        self.cam_info = {
            'device': '—', 'resolution': '—', 'fps': 0,
            'format': '—', 'is_capture_card': False,
        }

        # MJPEG preview frame
        self._frame = None
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()

        # Recording
        self._write_queue = queue.Queue(maxsize=120)
        self._stop = threading.Event()

        # Camera switch
        self._switch_requested = None
        self._switch_lock = threading.Lock()

        # Prevent multiple threads racing to open the camera
        self._open_lock = threading.Lock()

        # Recording state (set externally by DeviceManager)
        self.recording = False
        self.session_dir = None
        self.rec_start = None
        
        # Dynamic FPS Measurement
        self.measured_fps = 30.0
        self._fps_timestamps = []

    # ── Device Scanning ──────────────────────────────────────────

    @staticmethod
    def scan_video_devices():
        """
        Scan /dev/video* using v4l2-ctl ONLY (no OpenCV opens).
        Avoids device lock conflicts with the active camera loop.
        Filters out metadata-only nodes.
        """
        devices = []
        for path in sorted(glob.glob('/dev/video*')):
            idx_match = re.search(r'video(\d+)', path)
            if not idx_match:
                continue
            idx = int(idx_match.group(1))
            info = {
                'index': idx, 'path': path, 'name': path,
                'is_capture_card': False, 'resolutions': [],
            }

            has_video_capture = False
            try:
                out = subprocess.check_output(
                    ['v4l2-ctl', '-d', path, '--all'],
                    stderr=subprocess.DEVNULL, timeout=1
                ).decode('utf-8', errors='replace')

                in_device_caps = False
                for line in out.splitlines():
                    stripped = line.strip()
                    if 'Card type' in line:
                        card = line.split(':', 1)[1].strip()
                        info['name'] = card
                        cc_keywords = [
                            'capture', 'hdmi', 'cam link', 'elgato',
                            'magewell', 'avermedia', 'usb video',
                            'macrosilicon', 'ms2109', 'ms2130',
                            'sony', 'ilce', 'ilme', 'canon', 'eos',
                            'nikon', 'fuji', 'panasonic', 'lumix',
                            'display capture', 'uvc',
                        ]
                        if any(k in card.lower() for k in cc_keywords):
                            info['is_capture_card'] = True
                    if 'Device Caps' in line:
                        in_device_caps = True
                    elif in_device_caps:
                        if stripped == 'Video Capture':
                            has_video_capture = True
                        elif stripped and not stripped.startswith(
                                ('Streaming', 'Extended', 'Metadata')):
                            in_device_caps = False
            except (subprocess.SubprocessError, FileNotFoundError):
                has_video_capture = True

            if not has_video_capture:
                continue

            # Get resolutions via v4l2-ctl
            try:
                fmt_out = subprocess.check_output(
                    ['v4l2-ctl', '-d', path, '--list-formats-ext'],
                    stderr=subprocess.DEVNULL, timeout=1
                ).decode('utf-8', errors='replace')
                seen = set()
                for line in fmt_out.splitlines():
                    m = re.search(r'(\d{3,5})x(\d{3,5})', line)
                    if m:
                        w, h = int(m.group(1)), int(m.group(2))
                        key = f'{w}x{h}'
                        if key not in seen:
                            seen.add(key)
                            info['resolutions'].append(key)
                        if w >= 1920 or h >= 1080:
                            info['is_capture_card'] = True
            except (subprocess.SubprocessError, FileNotFoundError):
                pass

            devices.append(info)
        return devices

    # ── Source Resolution ────────────────────────────────────────

    def _resolve_source(self):
        """Resolve camera source to an OpenCV-compatible device identifier."""
        source = self.source

        if isinstance(source, int):
            return source
        if isinstance(source, str) and source.startswith('/dev/'):
            idx_match = re.search(r'video(\d+)', source)
            return int(idx_match.group(1)) if idx_match else source
        if isinstance(source, str) and source.isdigit():
            return int(source)

        # Auto-detect: fast-path — try /dev/video0 first without full scan
        if source == 'auto':
            # Quick check: does /dev/video0 exist and can OpenCV open it?
            fast_ids = []
            for path in sorted(glob.glob('/dev/video*'))[:6]:  # Only first 6 nodes
                m = re.search(r'video(\d+)', path)
                if m:
                    fast_ids.append(int(m.group(1)))
            if fast_ids:
                return fast_ids[0]  # Return immediately — skip slow v4l2 scan
            return 0

        return 0

    # ── Main Capture Loop ────────────────────────────────────────

    def run(self):
        """
        Main camera capture loop — rock-solid during recording.

        Stability improvements:
        - cap.read() instead of grab+retrieve (more resilient for USB)
        - 300-frame failure threshold (~15s) before reconnect (was 30 = 1.5s)
        - During recording: NEVER force-reconnect; drain stale V4L2 buffer instead
        - Buffer=4 absorbs brief USB hiccups without counting as failures
        - ok=True kept during stalls so overlay never appears mid-recording
        - Recovery message emitted when camera returns after stall
        """
        self._stop.clear()
        target_w, target_h = self.resolution
        target_fps = 30.0

        writer_thread = threading.Thread(
            target=self._writer_loop,
            args=(target_w, target_h, target_fps),
            daemon=True, name='cam-writer',
        )
        writer_thread.start()

        _jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, 65]
        consecutive_failures = 0
        # Failure thresholds (each failure = ~50ms):
        #   WARN      20  ≈  1s → warn frontend but keep ok=True
        #   RECONNECT 60  ≈  3s → force reconnect (idle only)
        #   RECORD    300 ≈ 15s → reconnect even during recording
        WARN_THRESHOLD      = 20
        RECONNECT_THRESHOLD = 60
        RECORDING_THRESHOLD = 300
        cap = None
        _reconnect_attempts = 0

        try:
            while not self._stop.is_set():

                # ── Camera switch request ────────────────────────
                with self._switch_lock:
                    if self._switch_requested is not None:
                        self.source = self._switch_requested
                        self._switch_requested = None
                        if cap is not None:
                            cap.release()
                            cap = None
                            consecutive_failures = 0

                # ── Open camera ──────────────────────────────────
                if cap is None:
                    device_id = self._resolve_source()
                    self.registry.set_state(
                        'camera', SensorState.SCANNING,
                        f'Connecting to camera (device {device_id})...'
                    )
                    self.sio.emit('device_status', {
                        'device': 'camera', 'ok': False,
                        'msg': f'Connecting to camera (device {device_id})...',
                    })

                    _devnull_fd = os.open(os.devnull, os.O_WRONLY)
                    _old_stderr = os.dup(2)
                    try:
                        os.dup2(_devnull_fd, 2)
                        cap = cv2.VideoCapture(device_id, cv2.CAP_V4L2)
                        if not cap.isOpened():
                            if cap is not None:
                                cap.release()
                            cap = cv2.VideoCapture(device_id)
                    finally:
                        os.dup2(_old_stderr, 2)
                        os.close(_devnull_fd)
                        os.close(_old_stderr)

                    if not cap.isOpened():
                        if cap is not None:
                            cap.release()
                        cap = None
                        _backoff = min(60, 5 * (2 ** min(_reconnect_attempts, 3)))
                        _reconnect_attempts += 1
                        for _ in range(int(_backoff * 10)):
                            if self._stop.is_set():
                                return
                            time.sleep(0.1)
                        continue

                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                    cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  target_w)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, target_h)
                    cap.set(cv2.CAP_PROP_FPS, 30)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 4)  # absorb USB hiccups

                    actual_w   = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))  or target_w
                    actual_h   = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or target_h
                    actual_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                    raw_fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                    fourcc_str = ''.join(
                        chr((raw_fourcc >> (8 * i)) & 0xFF) for i in range(4)
                    )
                    is_capture_card = (actual_w >= 1920 or actual_h >= 1080)

                    self.cam_info = {
                        'device': f'/dev/video{device_id}' if isinstance(device_id, int) else str(device_id),
                        'resolution': f'{actual_w}x{actual_h}',
                        'fps': round(actual_fps),
                        'format': fourcc_str.strip(),
                        'is_capture_card': is_capture_card,
                        'record_res': f'{actual_w}x{actual_h}',
                        'capture_mode': 'BGR',
                    }

                    label = '📹 Capture Card' if is_capture_card else '🎥 Webcam'
                    msg = f'{label} {actual_w}×{actual_h} @ {int(actual_fps)}fps [{fourcc_str.strip()}]'

                    self.registry.set_state('camera', SensorState.STREAMING, msg)
                    self.registry.set_metadata('camera', self.cam_info)
                    self.sio.emit('device_status', {'device': 'camera', 'ok': True, 'msg': msg})
                    self.sio.emit('camera_info', self.cam_info)

                    consecutive_failures = 0
                    _reconnect_attempts = 0
                    warmup_frames = 2
                    frame_count = 0
                    preview_scale = actual_w > 1280
                    if preview_scale:
                        preview_w = 960
                        preview_h = int(actual_h * (preview_w / actual_w))
                    else:
                        preview_w, preview_h = actual_w, actual_h

                # ── Read frame (cap.read is more resilient for USB) ──
                try:
                    ret, raw = cap.read()
                except cv2.error:
                    ret, raw = False, None

                # ── Handle failures ──────────────────────────────────
                if not ret or raw is None or (hasattr(raw, 'size') and raw.size == 0):
                    consecutive_failures += 1
                    
                    # Instantly detect if the physical USB device file was disconnected/removed
                    device_path = f'/dev/video{device_id}' if isinstance(device_id, int) else str(device_id)
                    device_exists = os.path.exists(device_path) if device_path.startswith('/') else True
                    
                    if not device_exists:
                        # Physical disconnect: trigger instant reconnect loop without waiting for thresholds
                        self.sio.emit('device_status', {
                            'device': 'camera', 'ok': False,
                            'msg': 'Camera disconnected — physically unplugged or powered off.',
                        })
                        self.registry.set_state('camera', SensorState.DISCONNECTED, 'Physically disconnected')
                        if cap is not None:
                            cap.release()
                        cap = None
                        consecutive_failures = 0
                        # Backoff/sleep 1.0s before next outer loop iteration
                        for _ in range(10):
                            if self._stop.is_set():
                                break
                            time.sleep(0.1)
                        continue

                    time.sleep(0.05)

                    if consecutive_failures == WARN_THRESHOLD:
                        # Warn but keep ok=True — overlay stays hidden
                        self.sio.emit('device_status', {
                            'device': 'camera', 'ok': True,
                            'msg': 'USB stall — recovering...',
                        })

                    # During recording: never force-reconnect below RECORDING_THRESHOLD.
                    # Instead flush stale V4L2 buffer every 20 failures.
                    if self.recording and consecutive_failures < RECORDING_THRESHOLD:
                        if consecutive_failures % 20 == 0:
                            try:
                                for _ in range(8):
                                    cap.grab()
                            except Exception:
                                pass
                        continue

                    # Not recording (or hit RECORDING_THRESHOLD): force reconnect
                    threshold = RECONNECT_THRESHOLD if not self.recording else RECORDING_THRESHOLD
                    if consecutive_failures >= threshold:
                        label = 'Recording stall' if self.recording else 'Camera lost'
                        self.sio.emit('device_status', {
                            'device': 'camera', 'ok': False,
                            'msg': f'{label} — reconnecting...',
                        })
                        self.registry.set_state('camera', SensorState.ERROR, f'{label} — reconnecting...')
                        cap.release()
                        cap = None
                        consecutive_failures = 0
                        for _ in range(30):
                            if self._stop.is_set():
                                break
                            time.sleep(0.1)
                    continue

                # ── Successful frame ─────────────────────────────────
                if consecutive_failures >= WARN_THRESHOLD:
                    self.sio.emit('device_status', {
                        'device': 'camera', 'ok': True,
                        'msg': f'Camera recovered (stalled {consecutive_failures} frames)',
                    })
                consecutive_failures = 0
                frame_count += 1

                now_t = time.monotonic()
                self._fps_timestamps.append(now_t)
                self._fps_timestamps = [t for t in self._fps_timestamps if now_t - t <= 2.0]
                if len(self._fps_timestamps) > 5:
                    self.measured_fps = len(self._fps_timestamps) / (now_t - self._fps_timestamps[0])
                if frame_count % 30 == 0:
                    self.cam_info['fps'] = round(self.measured_fps, 1)

                if frame_count <= warmup_frames:
                    continue

                # ── Preview (throttled) ──────────────────────────────
                if frame_count % 2 == 0 or actual_fps <= 15:
                    try:
                        preview = cv2.resize(raw, (preview_w, preview_h),
                                             interpolation=cv2.INTER_LINEAR) if preview_scale else raw
                        ok, jpg = cv2.imencode('.jpg', preview, _jpeg_params)
                        if ok and jpg is not None:
                            with self._frame_lock:
                                self._frame = jpg.tobytes()
                            self._frame_event.set()
                    except cv2.error:
                        pass

                # ── Recording queue ──────────────────────────────────
                if self.recording and self.session_dir:
                    t = time.monotonic() - self.rec_start
                    try:
                        self._write_queue.put_nowait((raw, t))
                    except queue.Full:
                        pass

                time.sleep(0.001)

        except Exception as e:
            self.sio.emit('device_status', {'device': 'camera', 'ok': False, 'msg': str(e)})
        finally:
            if cap is not None:
                cap.release()
            self._write_queue.put(None)
            writer_thread.join(timeout=5)
            self.registry.set_state('camera', SensorState.DISCONNECTED)

    def _writer_loop(self, width, height, fps):
        """
        Dedicated thread for recording I/O. Receives (frame, timestamp)
        tuples from the capture loop via _write_queue.
        Uses Frame Rate Constantization (FRC) to guarantee recorded video duration
        matches real-world duration exactly.
        """
        video_writer = None
        csv_f = None
        csv_w = None
        local_frames = 0
        cam_dir = None
        
        # Determine standard playback frame rate (FRC)
        target_fps = float(fps)
        if target_fps <= 0.0 or target_fps > 120.0:
            target_fps = 30.0
            
        last_written_idx = -1
        last_frame = None

        def _close_files():
            nonlocal video_writer, csv_f, csv_w, last_written_idx, last_frame
            if video_writer:
                video_writer.release()
                video_writer = None
            if csv_f:
                csv_f.flush()
                csv_f.close()
                csv_f = None
                csv_w = None
            last_written_idx = -1
            last_frame = None

        try:
            while True:
                try:
                    item = self._write_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if item is None:
                    break

                if item == 'STOP_REC':
                    _close_files()
                    continue

                frame, timestamp = item

                # Open files on first frame
                if csv_f is None and self.session_dir:
                    f_height, f_width = frame.shape[:2]
                    # Dynamically set target_fps to match actual measured frame rate
                    target_fps = float(getattr(self, 'measured_fps', fps))
                    if target_fps <= 0.0 or target_fps > 120.0:
                        target_fps = 30.0

                    cam_dir = os.path.join(self.session_dir, 'camera')
                    csv_f = open(
                        os.path.join(cam_dir, 'timestamps.csv'),
                        'w', newline='',
                    )
                    csv_w = csv.writer(csv_f)
                    csv_w.writerow(['frame_idx', 'timestamp_s', 'filename'])
                    local_frames = 0
                    last_written_idx = -1
                    last_frame = None

                    if self.record_format == 'video':
                        # Try highly compatible codecs on Linux (prefer fast MJPG first to avoid CPU bottleneck at 4K)
                        for codec, ext in [('MJPG', '.avi'), ('mp4v', '.mp4'), ('XVID', '.avi'), ('MJPG', '.mp4')]:
                            vw = cv2.VideoWriter(
                                os.path.join(cam_dir, f'recording{ext}'),
                                cv2.VideoWriter_fourcc(*codec),
                                target_fps, (f_width, f_height),
                            )
                            if vw.isOpened():
                                video_writer = vw
                                self.sio.emit('device_status', {
                                    'device': 'camera', 'ok': True,
                                    'msg': f'Recording {f_width}×{f_height} [{codec}] at {target_fps} FPS',
                                })
                                break
                            vw.release()

                # Write the frame (incorporating FRC to preserve perfect temporal accuracy)
                if self.record_format == 'video' and video_writer:
                    target_idx = max(last_written_idx + 1, int(timestamp * target_fps))
                    
                    if last_written_idx == -1:
                        video_writer.write(frame)
                        csv_w.writerow([0, f'{timestamp:.4f}', 'recording_frame_0'])
                        last_written_idx = 0
                    else:
                        # Fill gaps (duplicate last frame if there was any capture lag)
                        while last_written_idx < target_idx - 1:
                            duplicate_ts = (last_written_idx + 1) / target_fps
                            video_writer.write(last_frame if last_frame is not None else frame)
                            csv_w.writerow([
                                last_written_idx + 1,
                                f'{duplicate_ts:.4f}',
                                f'recording_frame_{last_written_idx + 1}',
                            ])
                            last_written_idx += 1
                        
                        # Write current frame
                        video_writer.write(frame)
                        csv_w.writerow([
                            target_idx,
                            f'{timestamp:.4f}',
                            f'recording_frame_{target_idx}',
                        ])
                        last_written_idx = target_idx
                    
                    last_frame = frame.copy()
                    local_frames = last_written_idx + 1
                    self.registry.set_counter('camera', local_frames)
                    
                elif csv_f:
                    fn = f'frame_{local_frames:06d}.jpg'
                    cv2.imwrite(
                        os.path.join(cam_dir, fn), frame,
                        [cv2.IMWRITE_JPEG_QUALITY, 95],
                    )
                    csv_w.writerow([local_frames, f'{timestamp:.4f}', fn])
                    local_frames += 1
                    self.registry.set_counter('camera', local_frames)
        finally:
            _close_files()

    # ── Public API ───────────────────────────────────────────────

    def switch_camera(self, new_source):
        """Request a camera switch (called from socket event)."""
        with self._switch_lock:
            self._switch_requested = new_source

    def gen_mjpeg(self):
        """Event-driven MJPEG generator for browser preview."""
        while True:
            self._frame_event.wait(timeout=0.1)
            self._frame_event.clear()
            with self._frame_lock:
                frame = self._frame
            if frame:
                yield (
                    b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
                )

    def stop_recording_files(self):
        """Signal writer thread to flush and close recording files."""
        try:
            self._write_queue.put('STOP_REC', timeout=2)
        except queue.Full:
            pass

    def stop(self):
        """Signal the capture loop to stop."""
        self._stop.set()
