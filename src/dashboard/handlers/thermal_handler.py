"""
Thermal Handler — live thermal capture on the master clock
==========================================================
Captures a thermal camera **live** and writes lossless frames + per-frame
master-clock timestamps, mirroring the RGB camera's write contract so the sync
audit treats it identically.

Why live capture solves the sync problem
----------------------------------------
The FLIR C5 (and similar handhelds) don't expose their internal radiometric files
in real time — but they DO give a live picture over USB video or by screen-sharing
their display. We grab that live stream **on the PC** and timestamp each frame with
``monotonic() - rec_start`` at the moment we read it. So thermal lands directly on
the master clock — there is no camera-RTC offset to reconcile (that problem only
exists for the offline file-offload path, which we are not using).

Two live sources (choose via `source`):
    - **UVC / V4L2** device — ``source='/dev/video2'`` / an int / ``'auto'``.
      Read with OpenCV, exactly like a webcam.
    - **Screen region** — ``source='screen'`` (full primary monitor) or
      ``'screen:LEFT,TOP,WIDTH,HEIGHT'`` to crop the shared thermal window. Read
      with `mss`. Use a tight region so the saved frames are just the thermal image.

Fidelity rules (brief §1)
-------------------------
    - **Real frames only** — every row is a genuinely-grabbed frame at its true
      grab time; NO frame-rate constantization / duplication (`is_real` is always
      1, kept only so the schema matches the RGB camera).
    - **Lossless** — frames are written as PNG.

Caveat — colorized, not radiometric
------------------------------------
A live UVC/screen feed gives the **palette-colorized** thermal image, not absolute
°C radiometric data. That preserves spatial thermal patterns (e.g. facial thermal
dynamics) but not calibrated temperature. If you need absolute °C, that requires
FLIR radiometric file offload (a separate offline path).

`cv2` / `mss` are imported lazily, so importing this module needs neither.
"""

import csv
import glob
import os
import queue
import re
import threading
import time

from src.recorder.sensor_registry import SensorRegistry, SensorState


def parse_thermal_source(source):
    """Resolve `source` → (kind, spec). Pure function (unit-tested).

    kind='none'   spec=None            → modality disabled
    kind='screen' spec=None            → full primary monitor
    kind='screen' spec={left,top,width,height}
    kind='v4l2'   spec=<device index or path>
    """
    if source in (None, 'none', ''):
        return ('none', None)
    if isinstance(source, str) and source.startswith('screen'):
        if ':' in source:
            parts = source.split(':', 1)[1].split(',')
            if len(parts) == 4:
                try:
                    l, t, w, h = (int(x) for x in parts)
                    return ('screen', {'left': l, 'top': t, 'width': w, 'height': h})
                except ValueError:
                    pass
        return ('screen', None)
    if isinstance(source, int):
        return ('v4l2', source)
    if isinstance(source, str) and source.startswith('/dev/'):
        m = re.search(r'video(\d+)', source)
        return ('v4l2', int(m.group(1)) if m else source)
    if isinstance(source, str) and source.isdigit():
        return ('v4l2', int(source))
    if source == 'auto':
        nodes = sorted(glob.glob('/dev/video*'))
        m = re.search(r'video(\d+)', nodes[0]) if nodes else None
        return ('v4l2', int(m.group(1)) if m else 0)
    return ('v4l2', 0)


def parse_crop(spec):
    """Parse a crop spec into {top,bottom,left,right} pixels-from-edge, or None.
    Accepts a dict, or a 'top,bottom,left,right' string (e.g. '0,20,0,45' to drop
    the FLIR logo strip + temperature scale bar). Pure function."""
    if not spec:
        return None
    if isinstance(spec, dict):
        return {k: int(spec.get(k, 0)) for k in ('top', 'bottom', 'left', 'right')}
    if isinstance(spec, str):
        parts = spec.split(',')
        if len(parts) == 4:
            try:
                t, b, l, r = (int(x) for x in parts)
                return {'top': t, 'bottom': b, 'left': l, 'right': r}
            except ValueError:
                return None
    return None


# ── frame sources (lazy heavy imports live inside open/read) ──────────────────

class _V4L2Source:
    """OpenCV capture of a UVC/V4L2 thermal video node."""
    def __init__(self, device_id):
        self.device_id = device_id
        self._cap = None
        self.resolution = '—'

    def open(self):
        os.environ.setdefault('OPENCV_LOG_LEVEL', 'SILENT')
        import cv2
        devnull = os.open(os.devnull, os.O_WRONLY)
        old = os.dup(2)
        try:
            os.dup2(devnull, 2)
            cap = cv2.VideoCapture(self.device_id, cv2.CAP_V4L2)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(self.device_id)
        finally:
            os.dup2(old, 2)
            os.close(devnull)
            os.close(old)
        if not cap.isOpened():
            cap.release()
            return False
        try:
            cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
        except cv2.error:
            pass
        self._cap = cap
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.resolution = f'{w}x{h}'
        return True

    def read(self):
        import cv2
        try:
            return self._cap.read()
        except cv2.error:
            return False, None

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class _ScreenSource:
    """Screen-region grab (for a screen-shared thermal window) via `mss`."""
    def __init__(self, region):
        self.region = region             # dict or None (full primary monitor)
        self._sct = None
        self._np = None
        self._mon = None
        self.resolution = '—'

    def open(self):
        import mss
        import numpy as np
        self._sct = mss.mss()
        self._np = np
        self._mon = self.region or self._sct.monitors[1]
        self.resolution = f"{self._mon['width']}x{self._mon['height']}"
        return True

    def read(self):
        try:
            img = self._sct.grab(self._mon)
            frame = self._np.array(img)[:, :, :3]   # BGRA → BGR
            return True, frame
        except Exception:
            return False, None

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


class ThermalHandler:
    """Live thermal capture loop + decoupled writer thread (bounded queue)."""

    def __init__(self, registry: SensorRegistry, sio, source='none',
                 resolution=(256, 192), fourcc='YUYV', crop=None):
        self.registry = registry
        self.sio = sio
        self.source = source                 # 'none' | 'auto' | '/dev/videoN' | 'screen[:l,t,w,h]'
        self.resolution = resolution
        self.fourcc = fourcc
        # Edge crop to strip the FLIR on-screen overlays (temp bar / logo) from the
        # RECORDED frames; the preview stays full-frame so you can measure them.
        self.crop = parse_crop(crop)

        self._write_queue = queue.Queue(maxsize=120)
        self._stop = threading.Event()

        # MJPEG preview (full frame). Enabled so the operator can watch the live
        # thermal feed and compare its refresh rate against the other modalities.
        self._frame = None
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()

        # Recording state (set externally by the daemon, exactly like the camera)
        self.recording = False
        self.session_dir = None
        self.rec_start = None
        self.cam_info = {'device': '—', 'resolution': '—', 'fps': 0}
        # Live capture fps (updated in monitoring too, so readiness can see the
        # stream before recording — the recording counter only advances on disk).
        self.measured_fps = 0.0
        self._fps_ts = []

    def _crop(self, frame):
        """Clean-crop a frame (remove the FLIR overlay edges) for recording."""
        c = self.crop
        if not c:
            return frame
        h, w = frame.shape[:2]
        top = c['top']
        left = c['left']
        bottom = (h - c['bottom']) if c['bottom'] else h
        right = (w - c['right']) if c['right'] else w
        if bottom <= top or right <= left:
            return frame                     # bad crop → don't destroy the frame
        return frame[top:bottom, left:right]

    def _make_source(self):
        kind, spec = parse_thermal_source(self.source)
        if kind == 'screen':
            return _ScreenSource(spec)
        return _V4L2Source(spec)

    # ── main capture loop ────────────────────────────────────────────────────

    def run(self):
        kind, _ = parse_thermal_source(self.source)
        if kind == 'none':
            self.registry.set_state('thermal', SensorState.DISABLED, 'Disabled')
            return

        import cv2                              # for preview JPEG encoding
        self._stop.clear()
        writer = threading.Thread(target=self._writer_loop, daemon=True,
                                  name='thermal-writer')
        writer.start()

        src = None
        fails = 0
        backoff = 0
        try:
            while not self._stop.is_set():
                if src is None:
                    self.registry.set_state('thermal', SensorState.SCANNING,
                                            f'Connecting thermal ({self.source})...')
                    src = self._make_source()
                    ok = False
                    try:
                        ok = src.open()
                    except Exception as e:
                        self.sio.emit('device_status',
                                      {'device': 'thermal', 'ok': False, 'msg': str(e)[:120]})
                    if not ok:
                        src = None
                        wait = min(30, 3 * (2 ** min(backoff, 3)))
                        backoff += 1
                        for _ in range(int(wait * 10)):
                            if self._stop.is_set():
                                return
                            time.sleep(0.1)
                        continue
                    self.cam_info = {'device': str(self.source),
                                     'resolution': src.resolution, 'fps': 0}
                    self.registry.set_state('thermal', SensorState.STREAMING,
                                            f'Thermal {src.resolution} [{kind}]')
                    self.registry.set_metadata('thermal', self.cam_info)
                    self.sio.emit('device_status',
                                  {'device': 'thermal', 'ok': True,
                                   'msg': f'Thermal {src.resolution}'})
                    backoff = 0
                    fails = 0

                ret, frame = src.read()
                if not ret or frame is None:
                    fails += 1
                    time.sleep(0.03)
                    if fails >= 100:              # ~3 s of failures → reconnect
                        src.close()
                        src = None
                        fails = 0
                    continue
                fails = 0

                # Live fps (monitoring + recording) for the readiness gate.
                now_t = time.monotonic()
                self._fps_ts.append(now_t)
                self._fps_ts = [x for x in self._fps_ts if now_t - x <= 2.0]
                if len(self._fps_ts) > 3:
                    self.measured_fps = len(self._fps_ts) / (now_t - self._fps_ts[0])
                    self.cam_info['fps'] = round(self.measured_fps, 1)

                # Preview from the FULL frame (overlays visible → you can measure the
                # crop) — thermal is slow (~9 Hz), so encoding every frame is cheap.
                try:
                    ok_j, jpg = cv2.imencode('.jpg', frame,
                                             [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok_j:
                        with self._frame_lock:
                            self._frame = jpg.tobytes()
                        self._frame_event.set()
                except cv2.error:
                    pass

                if self.recording and self.session_dir:
                    t = time.monotonic() - self.rec_start
                    try:
                        # Recorded frame is clean-cropped (overlays removed).
                        self._write_queue.put_nowait((self._crop(frame), t))
                    except queue.Full:
                        pass                     # drop rather than block capture
                time.sleep(0.001)
        except Exception as e:
            self.sio.emit('device_status',
                          {'device': 'thermal', 'ok': False, 'msg': str(e)})
        finally:
            if src is not None:
                src.close()
            self._write_queue.put(None)
            writer.join(timeout=5)
            self.registry.set_state('thermal', SensorState.DISCONNECTED)

    def _writer_loop(self):
        """Write each real thermal frame losslessly + one timestamps.csv row."""
        import cv2
        csv_f = csv_w = None
        idx = 0
        tdir = None
        try:
            while True:
                try:
                    item = self._write_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    break
                if item == 'STOP_REC':
                    if csv_f:
                        csv_f.flush()
                        csv_f.close()
                        csv_f = csv_w = None
                    idx = 0
                    continue
                frame, t = item
                if csv_f is None and self.session_dir:
                    tdir = os.path.join(self.session_dir, 'thermal')
                    os.makedirs(tdir, exist_ok=True)
                    csv_f = open(os.path.join(tdir, 'timestamps.csv'),
                                 'a', newline='', buffering=1)
                    csv_w = csv.writer(csv_f)
                    if csv_f.tell() == 0:
                        # is_real is always 1: no FRC/duplication for thermal.
                        csv_w.writerow(['frame_idx', 'timestamp_s',
                                        'filename', 'is_real'])
                    idx = 0
                fn = f'frame_{idx:06d}.png'
                try:
                    cv2.imwrite(os.path.join(tdir, fn), frame)   # PNG = lossless
                except cv2.error:
                    continue
                csv_w.writerow([idx, f'{t:.4f}', fn, 1])
                idx += 1
                self.registry.set_counter('thermal', idx)
        finally:
            if csv_f:
                csv_f.flush()
                csv_f.close()

    # ── public API (mirrors CameraHandler) ────────────────────────────────────

    def gen_mjpeg(self):
        """Event-driven MJPEG generator for the browser thermal preview."""
        while True:
            self._frame_event.wait(timeout=0.5)
            self._frame_event.clear()
            with self._frame_lock:
                frame = self._frame
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

    def stop_recording_files(self):
        try:
            self._write_queue.put('STOP_REC', timeout=2)
        except queue.Full:
            pass

    def stop(self):
        self._stop.set()
