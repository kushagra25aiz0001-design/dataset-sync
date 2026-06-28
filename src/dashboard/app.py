"""
Live Dashboard — Dataset Sync
==============================
Real-time web dashboard for monitoring camera, oximeter, WiFi CSI,
EMG, and GSR data during recording sessions.

Modes:
    GUI mode (default):
        python -m src.dashboard
    Headless mode (no browser, no Flask — saves RAM):
        python -m src.dashboard --headless --duration 120 --subject test01

Architecture:
    - DeviceManager: thin coordinator delegating to handler modules
    - SensorRegistry: thread-safe centralized state
    - NullSIO: drop-in silent emitter for headless mode
    - Handler modules: one per sensor (camera, oximeter, csi, emg, gsr)
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler

from src.recorder.sensor_registry import SensorRegistry, SensorState
from src.recorder.sensor_orchestrator import SensorOrchestrator
from src.dashboard.handlers.camera_handler import CameraHandler
from src.dashboard.handlers.oximeter_handler import OximeterHandler
from src.dashboard.handlers.csi_handler import CSIHandler
from src.dashboard.handlers.emg_handler import EMGHandler
from src.dashboard.handlers.gsr_handler import GSRHandler


# ─── Null SocketIO (headless mode) ────────────────────────────

class NullSIO:
    """
    Silent drop-in replacement for Flask-SocketIO.
    All emit() calls are silently discarded — zero RAM overhead.
    Handlers use self.sio.emit() everywhere; in headless mode this
    object is passed instead of the real SocketIO instance.
    """
    def emit(self, *_args, **_kwargs):
        pass

    def run(self, *_args, **_kwargs):
        pass


# ─── Flask App (lazy init — only in GUI mode) ────────────────

BASE_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..', '..'))

app = None
socketio = None

MAX_SESSION_HOURS = 8  # hard cap: no single recording session exceeds 8 hours

# Conservative floor sample rates (Hz) — deliberately well below nominal.
# Used only to flag *dead/broken* sensors, not to penalize known throughput limits.
SENSOR_MIN_HZ = {
    'camera':   5.0,
    'oximeter': 20.0,
    'csi':      10.0,
    'emg':      50.0,
    'gsr':      3.0,
}


def _count_data_rows(csv_path: str) -> int:
    """Count CSV data rows (lines whose first char is a digit). Header/comment-safe."""
    if not os.path.exists(csv_path):
        return 0
    n = 0
    try:
        with open(csv_path, 'r', errors='replace') as fh:
            for line in fh:
                s = line.lstrip()
                if s and s[0].isdigit():
                    n += 1
    except OSError:
        return 0
    return n


def _scan_gaps(csv_path, ts_col, nominal_dt, is_real_col=None, max_listed=20):
    """
    Stream a CSV and find timestamp gaps — intervals far larger than the nominal
    sampling period (a dropout/disconnect). O(1) memory. Returns a dict:
        {count, max_gap_s, total_gap_s, gaps: [{at_s, gap_s}, ...]}.
    `ts_col` is the timestamp column index (seconds); `is_real_col`, if given,
    restricts to rows whose value in that column is '1' (camera real frames).
    """
    threshold = max(0.5, 5.0 * nominal_dt)
    prev = None
    count = 0
    max_gap = 0.0
    total_gap = 0.0
    gaps = []
    if not os.path.exists(csv_path):
        return {'count': 0, 'max_gap_s': 0.0, 'total_gap_s': 0.0, 'gaps': []}
    try:
        with open(csv_path, 'r', errors='replace') as fh:
            for line in fh:
                s = line.lstrip()
                if not s or not s[0].isdigit():
                    continue
                parts = s.rstrip('\n').split(',')
                if is_real_col is not None:
                    if len(parts) <= is_real_col or parts[is_real_col].strip() != '1':
                        continue
                if len(parts) <= ts_col:
                    continue
                try:
                    t = float(parts[ts_col])
                except ValueError:
                    continue
                if prev is not None:
                    gap = t - prev
                    if gap > threshold:
                        count += 1
                        total_gap += gap
                        if gap > max_gap:
                            max_gap = gap
                        if len(gaps) < max_listed:
                            gaps.append({'at_s': round(prev, 3),
                                         'gap_s': round(gap, 3)})
                prev = t
    except OSError:
        pass
    return {'count': count, 'max_gap_s': round(max_gap, 3),
            'total_gap_s': round(total_gap, 3), 'gaps': gaps}


def _check_disk_space(path: str, min_gb: float = 10.0):
    """Return (free_gb, is_ok). is_ok=False when free space drops below min_gb."""
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024 ** 3)
    return free_gb, free_gb >= min_gb


def _init_flask():
    """Initialize Flask + SocketIO only when GUI mode is used."""
    global app, socketio
    from flask import Flask
    from flask_socketio import SocketIO as RealSocketIO

    app = Flask(__name__,
                template_folder=os.path.join(BASE_DIR, 'templates'),
                static_folder=os.path.join(BASE_DIR, 'static'))
    app.config['SECRET_KEY'] = 'dataset-sync-live'
    socketio = RealSocketIO(app, cors_allowed_origins="*",
                            async_mode='threading')

    # Rotating log: 5 MB × 3 files max; Werkzeug at WARNING to suppress routine GETs
    _log_path = os.path.join(PROJECT_ROOT, 'dashboard.log')
    _log_handler = RotatingFileHandler(_log_path, maxBytes=5 * 1024 * 1024, backupCount=3)
    _log_handler.setLevel(logging.WARNING)
    logging.getLogger('werkzeug').addHandler(_log_handler)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    _register_routes()
    _register_socket_events()
    return app, socketio


# ─── Device Manager ──────────────────────────────────────────

class DeviceManager:
    """
    Thin coordinator that delegates all sensor I/O to handler modules.
    Works identically in GUI and headless mode — the only difference
    is whether `sio` is a real SocketIO or a NullSIO.
    """

    def __init__(self, sio, camera_source='auto', cam_res=(1920, 1080),
                 record_format='video', oxi_port='auto',
                 csi_port='/dev/ttyUSB1', csi_baud=115200,
                 emg_port='auto', emg_baud=230400,
                 gsr_port='auto', gsr_baud=115200):
        self.sio = sio
        self.record_format = record_format

        self.registry = SensorRegistry()
        self.orchestrator = SensorOrchestrator(self.registry, sio)

        self.camera = CameraHandler(
            self.registry, sio, source=camera_source,
            resolution=cam_res, record_format=record_format,
        )
        self.oximeter = OximeterHandler(self.registry, sio, port_cfg=oxi_port)
        self.csi = CSIHandler(self.registry, sio, port=csi_port, baud=csi_baud)
        self.emg = EMGHandler(self.registry, sio, port=emg_port, baud=emg_baud)
        self.gsr = GSRHandler(self.registry, sio, port=gsr_port, baud=gsr_baud)

        self.monitoring = False
        self.recording = False
        self.session_dir = None
        self.session_id = None
        self.rec_start = None
        self.rec_duration = 0
        self.rec_subject = ''
        self._stop = threading.Event()
        self._threads = {}

        # Live data-quality monitoring
        self._sensor_rates = {}          # sensor → rolling Hz
        self._qa_last_counts = {}        # sensor → last sampled counter
        self._qa_last_t = None
        self._qa_thread = None
        self._qa_stop = threading.Event()
        self.last_quality_report = None

    # ── Properties ───────────────────────────────────────────────

    @property
    def cam_ok(self):
        return self.registry.is_ok('camera')

    @property
    def oxi_ok(self):
        return self.registry.is_ok('oximeter')

    @property
    def csi_ok(self):
        return self.registry.is_ok('csi')

    @property
    def emg_ok(self):
        return self.registry.is_ok('emg')

    @property
    def gsr_ok(self):
        return self.registry.is_ok('gsr')

    @property
    def cam_frames(self):
        return self.registry.get_counter('camera')

    @property
    def oxi_samples(self):
        return self.registry.get_counter('oximeter')

    @property
    def csi_packets(self):
        return self.registry.get_counter('csi')

    @property
    def emg_packets(self):
        return self.registry.get_counter('emg')

    @property
    def gsr_samples(self):
        return self.registry.get_counter('gsr')

    @property
    def cam_info(self):
        return self.camera.cam_info

    # ── Retry Wrapper ────────────────────────────────────────────

    def _sensor_retry_wrapper(self, device_name, handler,
                              max_retries=5, retry_delay=8):
        attempt = 0
        while not self._stop.is_set() and attempt < max_retries:
            try:
                handler.run()
                break
            except Exception as e:
                attempt += 1
                if self._stop.is_set():
                    break
                msg = f'Error (attempt {attempt}/{max_retries}): {str(e)[:100]}'
                self.registry.set_state(device_name, SensorState.ERROR, msg)
                self.sio.emit('device_status', {
                    'device': device_name, 'ok': False, 'msg': msg,
                })
                if attempt < max_retries:
                    for _ in range(retry_delay * 2):
                        if self._stop.is_set():
                            return
                        time.sleep(0.5)

        if attempt >= max_retries and not self._stop.is_set():
            self.sio.emit('device_status', {
                'device': device_name, 'ok': False,
                'msg': f'Gave up after {max_retries} attempts',
            })

    # ── Live Data-Quality Monitoring ─────────────────────────────

    def _enabled_sensors(self):
        """Sensor names that the user has NOT disabled (state != DISABLED)."""
        names = []
        for name in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
            info = self.registry.get_sensor(name)
            if info and info.state != SensorState.DISABLED:
                names.append(name)
        return names

    def _qa_loop(self):
        """
        Background thread: once per second, compute each sensor's rolling
        sample rate (Hz) from its counter delta and emit a `qa_update` so the
        UI can show a live data-quality strip. Flags stalled sensors during
        recording (data expected but none arriving).
        """
        self._qa_last_t = time.monotonic()
        for name in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
            self._qa_last_counts[name] = self.registry.get_counter(name)

        while not self._qa_stop.is_set():
            self._qa_stop.wait(1.0)
            if self._qa_stop.is_set():
                break
            now = time.monotonic()
            dt = max(1e-3, now - (self._qa_last_t or now))
            self._qa_last_t = now

            rates, stalled = {}, []
            for name in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
                cur = self.registry.get_counter(name)
                delta = max(0, cur - self._qa_last_counts.get(name, cur))
                self._qa_last_counts[name] = cur
                hz = round(delta / dt, 1)
                rates[name] = hz
                self._sensor_rates[name] = hz

            enabled = self._enabled_sensors()
            if self.recording:
                for name in enabled:
                    if self._sensor_rates.get(name, 0) < SENSOR_MIN_HZ[name]:
                        stalled.append(name)

            elapsed = 0.0
            if self.recording and self.rec_start:
                elapsed = round(now - self.rec_start, 1)

            self.sio.emit('qa_update', {
                'rates': rates,
                'stalled': stalled,
                'recording': self.recording,
                'elapsed': elapsed,
                'duration': self.rec_duration,
            })

    def get_readiness(self):
        """
        Per-sensor readiness for the pre-flight gate. A sensor is 'ready' when
        it is connected/streaming AND producing data above its floor rate.
        Disabled sensors are reported as not-required.
        """
        result = {}
        any_ready = False
        for name in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
            info = self.registry.get_sensor(name)
            if info is None or info.state == SensorState.DISABLED:
                result[name] = {'enabled': False, 'ready': False,
                                'rate_hz': 0.0, 'reason': 'disabled'}
                continue
            rate = self._sensor_rates.get(name, 0.0)
            streaming = info.state in (SensorState.CONNECTED, SensorState.STREAMING)
            ready = streaming and rate >= SENSOR_MIN_HZ[name]
            if ready:
                any_ready = True
                reason = 'ok'
            elif not streaming:
                reason = f'not streaming ({info.state.value})'
            else:
                reason = f'no data ({rate:.1f} Hz)'
            result[name] = {'enabled': True, 'ready': ready,
                            'rate_hz': rate, 'reason': reason}
        return {'sensors': result, 'any_ready': any_ready}

    def _validate_session(self, duration):
        """
        Post-session integrity check. Counts rows actually written to disk per
        sensor, compares against a conservative floor and against what the
        handler observed in RAM, and writes quality_report.json. Returns the
        report dict (also stored on self.last_quality_report).
        """
        if not self.session_dir:
            return None

        files = {
            'camera':   os.path.join(self.session_dir, 'camera', 'timestamps.csv'),
            'oximeter': os.path.join(self.session_dir, 'oximeter', 'oximeter_log.csv'),
            'csi':      os.path.join(self.session_dir, 'csi', 'csi_log.csv'),
            'emg':      os.path.join(self.session_dir, 'emg', 'emg_log.csv'),
            'gsr':      os.path.join(self.session_dir, 'gsr', 'gsr_log.csv'),
        }
        counters = {
            'camera': self.cam_frames, 'oximeter': self.oxi_samples,
            'csi': self.csi_packets, 'emg': self.emg_packets,
            'gsr': self.gsr_samples,
        }

        enabled = set(self._enabled_sensors())
        report = {'session_id': self.session_id, 'duration_s': round(duration, 1),
                  'sensors': {}, 'overall': 'green'}
        rank = {'green': 0, 'yellow': 1, 'red': 2}
        worst = 'green'

        for name, path in files.items():
            if name not in enabled:
                report['sensors'][name] = {'status': 'skipped', 'reason': 'disabled'}
                continue

            rows = _count_data_rows(path)
            seen = counters.get(name, 0)
            expected_min = duration * SENSOR_MIN_HZ[name]
            status, reason = 'green', 'ok'

            if rows == 0:
                status, reason = 'red', 'no data written to disk'
            elif rows < expected_min:
                status = 'yellow'
                reason = f'low row count: {rows} < {expected_min:.0f} expected'
            elif seen > 0 and rows < 0.5 * seen:
                status = 'yellow'
                reason = f'disk lag: {rows} rows on disk vs {seen} captured'

            # Camera: also require a non-empty video file (video format only)
            if name == 'camera' and self.record_format == 'video':
                cam_dir = os.path.join(self.session_dir, 'camera')
                vids = [f for f in os.listdir(cam_dir)
                        if f.startswith('recording.')] if os.path.isdir(cam_dir) else []
                vid_ok = any(os.path.getsize(os.path.join(cam_dir, v)) > 0 for v in vids)
                if not vid_ok:
                    status, reason = 'red', 'video file missing or empty'

            rate = round(rows / duration, 1) if duration > 0 else 0.0

            # Gap detection on the shared PC clock. CSI is checked via
            # csi_timestamped.csv (PC anchor), not the raw device-clock log.
            # Camera counts only real frames (is_real==1), skipping FRC fills.
            gap_info = {'count': 0, 'max_gap_s': 0.0, 'total_gap_s': 0.0, 'gaps': []}
            if rows > 1 and duration > 0:
                nominal_dt = duration / rows
                if name == 'camera':
                    gap_info = _scan_gaps(path, ts_col=1, nominal_dt=nominal_dt,
                                          is_real_col=3)
                elif name == 'csi':
                    csi_ts = os.path.join(self.session_dir, 'csi',
                                          'csi_timestamped.csv')
                    gap_info = _scan_gaps(csi_ts, ts_col=0, nominal_dt=nominal_dt)
                else:
                    gap_info = _scan_gaps(path, ts_col=0, nominal_dt=nominal_dt)

            if gap_info['count'] > 0 and status == 'green':
                status = 'yellow'
                reason = (f"{gap_info['count']} gap(s), "
                          f"max {gap_info['max_gap_s']}s — possible dropout")

            report['sensors'][name] = {
                'status': status, 'reason': reason,
                'rows_on_disk': rows, 'captured': seen,
                'effective_hz': rate,
                'gaps': gap_info['count'],
                'max_gap_s': gap_info['max_gap_s'],
                'total_gap_s': gap_info['total_gap_s'],
                'gap_detail': gap_info['gaps'],
            }
            if rank[status] > rank[worst]:
                worst = status

        report['overall'] = worst
        try:
            with open(os.path.join(self.session_dir, 'quality_report.json'), 'w') as f:
                json.dump(report, f, indent=2)
        except OSError:
            pass
        self.last_quality_report = report
        return report

    # ── Lifecycle ────────────────────────────────────────────────

    def start_monitoring(self):
        if self.monitoring:
            self.stop_monitoring()

        self._stop.clear()
        self.monitoring = True
        self._threads = {}

        handlers = [
            ('camera',   self.camera,   3, 5),
            ('csi',      self.csi,      3, 5),
            ('oximeter', self.oximeter, 5, 8),
            ('emg',      self.emg,      5, 8),
            ('gsr',      self.gsr,      5, 8),
        ]

        for name, handler, retries, delay in handlers:
            port_cfg = (getattr(handler, 'port_cfg', None)
                        or getattr(handler, 'port', None)
                        or getattr(handler, 'source', None))
            if port_cfg == 'none':
                self.registry.set_state(name, SensorState.DISABLED, 'Disabled')
                continue
            t = threading.Thread(
                target=self._sensor_retry_wrapper,
                args=(name, handler, retries, delay),
                daemon=True, name=name,
            )
            t.start()
            self._threads[name] = t

        # Live data-quality monitor (runs whenever monitoring is active)
        self._qa_stop.clear()
        self._qa_thread = threading.Thread(
            target=self._qa_loop, daemon=True, name='qa-monitor',
        )
        self._qa_thread.start()

    def stop_monitoring(self):
        self._stop.set()
        self._qa_stop.set()
        self.monitoring = False
        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr]:
            handler.stop()
        
        # Join all threads to ensure they exit and release their resources/locks
        # Oximeter has a 3s reconnect sleep (now interruptible); allow more time
        for name, t in list(self._threads.items()):
            if t.is_alive():
                t.join(timeout=5.0 if name == 'oximeter' else 2.0)
        self._threads.clear()

        if self._qa_thread and self._qa_thread.is_alive():
            self._qa_thread.join(timeout=2.0)
        self._qa_thread = None

    # ── Recording ────────────────────────────────────────────────

    def start_recording(self, subject, duration, force=False):
        # Clamp duration to a sane range
        duration = min(max(int(duration), 1), MAX_SESSION_HOURS * 3600)

        # Pre-flight disk space check (warn if <10 GB free)
        free_gb, space_ok = _check_disk_space(PROJECT_ROOT)
        if not space_ok:
            self.sio.emit('device_status', {
                'device': 'system', 'ok': False,
                'msg': f'Insufficient disk space: only {free_gb:.1f} GB free (need ≥10 GB)',
            })
            return None

        # Pre-flight readiness gate: refuse to record if no enabled sensor is
        # actually producing data (prevents "recording nothing"). `force`
        # bypasses the gate for deliberate edge cases.
        readiness = self.get_readiness()
        if not force and not readiness['any_ready']:
            not_ready = [f"{n}: {d['reason']}"
                         for n, d in readiness['sensors'].items() if d['enabled']]
            self.sio.emit('device_status', {
                'device': 'system', 'ok': False,
                'msg': 'No sensor is producing data — recording blocked. '
                       + ('; '.join(not_ready) if not_ready else 'all sensors disabled'),
            })
            self.sio.emit('rec_blocked', {'reason': 'not_ready',
                                          'readiness': readiness})
            return None

        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        ts = now.strftime("%Y%m%d_%H%M%S")
        self.session_id = f"session_{ts}"
        self.session_dir = os.path.join(
            PROJECT_ROOT, 'data', 'raw', self.session_id,
        )
        for sub in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
            os.makedirs(os.path.join(self.session_dir, sub), exist_ok=True)

        self.rec_start = time.monotonic()
        self.rec_duration = duration
        self.rec_subject = subject
        self.registry.reset_all_counters()
        self.recording = True

        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr]:
            # Set the timestamp origin and output dir BEFORE flipping `recording`
            # on, so a handler thread can never observe recording==True while
            # rec_start is still None (which would compute monotonic() - None).
            handler.session_dir = self.session_dir
            handler.rec_start = self.rec_start
            handler.recording = True

        meta = {
            'session_id': self.session_id,
            'subject': subject,
            'start': now.isoformat(),
            'duration_target': duration,
            'camera': self.cam_info.copy(),
            'record_format': self.record_format,
        }
        with open(os.path.join(self.session_dir, 'metadata.json'), 'w') as f:
            json.dump(meta, f, indent=2)

        # Auto-stop timer with periodic disk-space monitoring
        def _auto_stop():
            check_interval = 60  # check disk every 60 seconds
            elapsed = 0.0
            while elapsed < duration and self.recording:
                sleep_chunk = min(check_interval, duration - elapsed)
                for _ in range(int(sleep_chunk * 10)):
                    if not self.recording:
                        return
                    time.sleep(0.1)
                elapsed += sleep_chunk
                if not self.recording:
                    return
                free_gb, ok = _check_disk_space(self.session_dir or PROJECT_ROOT)
                if not ok:
                    self.sio.emit('device_status', {
                        'device': 'system', 'ok': False,
                        'msg': f'DISK FULL WARNING: {free_gb:.1f} GB left — stopping recording!',
                    })
                    self.stop_recording()
                    self.sio.emit('rec_stopped', {
                        'session': self.session_id,
                        'frames': self.cam_frames,
                        'oxi': self.oxi_samples,
                        'csi': self.csi_packets,
                        'emg': self.emg_packets,
                        'gsr': self.gsr_samples,
                    })
                    return
            if self.recording:
                self.stop_recording()
                self.sio.emit('rec_stopped', {
                    'session': self.session_id,
                    'frames': self.cam_frames,
                    'oxi': self.oxi_samples,
                    'csi': self.csi_packets,
                    'emg': self.emg_packets,
                    'gsr': self.gsr_samples,
                })
        threading.Thread(target=_auto_stop, daemon=True).start()
        return self.session_id

    def stop_recording(self):
        self.recording = False
        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr]:
            handler.recording = False
        self.camera.stop_recording_files()
        time.sleep(0.3)

        if self.session_dir:
            duration_actual = round(
                time.monotonic() - self.rec_start, 2,
            ) if self.rec_start else 0

            # Post-session integrity check → quality_report.json
            report = self._validate_session(duration_actual)

            meta_path = os.path.join(self.session_dir, 'metadata.json')
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                meta['duration_actual'] = duration_actual
                meta['stats'] = {
                    'cam': self.cam_frames, 'oxi': self.oxi_samples,
                    'csi': self.csi_packets, 'emg': self.emg_packets,
                    'gsr': self.gsr_samples,
                }
                if report:
                    meta['quality'] = report
                with open(meta_path, 'w') as f:
                    json.dump(meta, f, indent=2)

            if report:
                self.sio.emit('quality_report', report)

    # ── Camera Delegation ────────────────────────────────────────

    def switch_camera(self, new_source):
        self.camera.switch_camera(new_source)

    def gen_mjpeg(self):
        return self.camera.gen_mjpeg()

    @staticmethod
    def scan_video_devices():
        return CameraHandler.scan_video_devices()


# ─── Global instance ─────────────────────────────────────────────
dm: DeviceManager = None  # type: ignore


# ─── Routes (registered lazily in GUI mode) ──────────────────────

def _register_routes():
    from flask import render_template, Response, jsonify, request

    @app.route('/')
    def index():
        return render_template('dashboard.html')

    @app.route('/video_feed')
    def video_feed():
        if dm:
            return Response(dm.gen_mjpeg(),
                            mimetype='multipart/x-mixed-replace; boundary=frame')
        return '', 204

    @app.route('/api/status')
    def api_status():
        if not dm:
            return jsonify({})
        elapsed = 0
        if dm.recording and dm.rec_start:
            elapsed = round(time.monotonic() - dm.rec_start, 1)
        snapshot = dm.registry.get_status_snapshot()
        snapshot.update({
            'recording': dm.recording, 'session': dm.session_id,
            'elapsed': elapsed, 'duration': dm.rec_duration,
            'record_format': dm.record_format,
            'monitoring': dm.monitoring,
        })
        return jsonify(snapshot)

    @app.route('/api/camera_info')
    def api_camera_info():
        return jsonify(dm.cam_info if dm else {})

    @app.route('/api/readiness')
    def api_readiness():
        return jsonify(dm.get_readiness() if dm else {'sensors': {}, 'any_ready': False})

    @app.route('/api/quality_report')
    def api_quality_report():
        return jsonify(dm.last_quality_report if dm and dm.last_quality_report else {})

    @app.route('/api/ports')
    def api_ports():
        import serial.tools.list_ports
        ports = [{'device': p.device, 'description': p.description}
                 for p in serial.tools.list_ports.comports()
                 if 'ttyUSB' in p.device or 'ttyACM' in p.device]
        video = DeviceManager.scan_video_devices()
        return jsonify({'serial': ports, 'video': video})

    @app.route('/api/start_monitoring', methods=['POST'])
    def api_start_monitoring():
        if not dm:
            return jsonify({'error': 'Not initialized'}), 500
        data = request.json or {}
        dm.camera.source = data.get('camera', dm.camera.source)
        dm.oximeter.port_cfg = data.get('oximeter', 'none')
        dm.csi.port = data.get('csi', 'none')
        dm.emg.port_cfg = data.get('emg', 'none')
        dm.gsr.port_cfg = data.get('gsr', 'none')
        dm.start_monitoring()
        return jsonify({'success': True})

    @app.route('/api/scan_devices')
    def api_scan_devices():
        return jsonify({'devices': DeviceManager.scan_video_devices()})

    @app.route('/api/start_recording', methods=['GET', 'POST'])
    def api_start_recording():
        if not dm:
            return jsonify({'error': 'Not initialized'}), 500
        
        # Get parameters from POST json or GET query parameters
        if request.method == 'POST':
            data = request.json or {}
            subject = data.get('subject', 'unknown')
            duration = int(data.get('duration', 60))
            record_format = data.get('record_format', 'video')
            force = bool(data.get('force', False))
        else:
            subject = request.args.get('subject', 'unknown')
            duration = int(request.args.get('duration', 60))
            record_format = request.args.get('record_format', 'video')
            force = request.args.get('force', '').lower() in ('1', 'true', 'yes')

        if not dm.recording:
            if record_format in ('video', 'frames'):
                dm.record_format = record_format
                dm.camera.record_format = record_format
            sid = dm.start_recording(subject, duration, force=force)
            if sid is None:
                return jsonify({
                    'error': 'Recording not started — disk full or no sensor producing data',
                    'readiness': dm.get_readiness(),
                }), 409
            socketio.emit('rec_started', {'session': sid})
            return jsonify({
                'success': True,
                'session_id': sid,
                'message': f'Recording started: {sid} (Subject: {subject}, Duration: {duration}s)'
            })
        return jsonify({'error': 'Already recording'}), 400

    @app.route('/api/stop_recording', methods=['GET', 'POST'])
    def api_stop_recording():
        if not dm:
            return jsonify({'error': 'Not initialized'}), 500
        if dm.recording:
            dm.stop_recording()
            socketio.emit('rec_stopped', {
                'session': dm.session_id,
                'frames': dm.cam_frames, 'oxi': dm.oxi_samples,
                'csi': dm.csi_packets, 'emg': dm.emg_packets,
                'gsr': dm.gsr_samples,
            })
            return jsonify({
                'success': True,
                'message': 'Recording stopped'
            })
        return jsonify({'error': 'Not recording'}), 400



def _register_socket_events():
    @socketio.on('connect')
    def handle_connect():
        if dm:
            from flask_socketio import emit
            for sensor in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
                ok = dm.registry.is_ok(sensor)
                info = dm.registry.get_sensor(sensor)
                msg = info.status_msg if info else 'Unknown'
                emit('device_status', {'device': sensor, 'ok': ok, 'msg': msg})
            emit('camera_info', dm.cam_info)

    @socketio.on('start_rec')
    def handle_start(data):
        if dm and not dm.recording:
            fmt = data.get('record_format')
            if fmt in ('video', 'frames'):
                dm.record_format = fmt
                dm.camera.record_format = fmt
            sid = dm.start_recording(
                data.get('subject', 'unknown'),
                int(data.get('duration', 60)),
                force=bool(data.get('force', False)),
            )
            if sid is not None:
                socketio.emit('rec_started', {'session': sid})

    @socketio.on('stop_rec')
    def handle_stop(_data=None):
        if dm and dm.recording:
            dm.stop_recording()
            socketio.emit('rec_stopped', {
                'session': dm.session_id,
                'frames': dm.cam_frames, 'oxi': dm.oxi_samples,
                'csi': dm.csi_packets, 'emg': dm.emg_packets,
                'gsr': dm.gsr_samples,
            })

    @socketio.on('switch_camera')
    def handle_switch_camera(data):
        if dm:
            dm.switch_camera(data.get('source', 'auto'))

    @socketio.on('set_record_format')
    def handle_set_format(data):
        if dm:
            fmt = data.get('format', 'video')
            if fmt in ('video', 'frames'):
                dm.record_format = fmt
                dm.camera.record_format = fmt
                socketio.emit('record_format_changed', {'format': fmt})


# ─── Headless Runner ─────────────────────────────────────────────

def _run_headless(dm_instance, subject, duration, force=False):
    """
    Run sensors in headless mode — no Flask, no browser, no MJPEG.
    Prints compact terminal status every 2 seconds.
    Uses NullSIO so zero RAM is wasted on SocketIO queues.
    """
    dm_ref = dm_instance
    dm_ref.start_monitoring()

    # Wait a few seconds for sensors to connect (QA loop populates live rates)
    print('  ⏳ Waiting 5s for sensors to connect...')
    time.sleep(5)

    # Print sensor status with live data rate (readiness)
    readiness = dm_ref.get_readiness()
    for sensor in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
        info = dm_ref.registry.get_sensor(sensor)
        r = readiness['sensors'].get(sensor, {})
        if info and info.state == SensorState.DISABLED:
            icon = '⬛'
        elif r.get('ready'):
            icon = '✅'
        else:
            icon = '❌'
        msg = info.status_msg if info else 'Unknown'
        hz = r.get('rate_hz', 0.0)
        suffix = f'  ({hz:.1f} Hz)' if r.get('enabled') else ''
        print(f'  {icon} {sensor:>10}: {msg}{suffix}')
    print()

    # Start recording (readiness-gated unless --force)
    sid = dm_ref.start_recording(subject, duration, force=force)
    if sid is None:
        print('  🚫 Recording blocked: no sensor is producing data '
              '(disk full or all sensors dead).')
        print('     Fix the sensors and retry, or pass --force to override.')
        dm_ref.stop_monitoring()
        return
    print(f'  🔴 RECORDING: {sid}')
    print(f'  📁 Output: {dm_ref.session_dir}')
    print(f'  ⏱️  Duration: {duration}s')
    print()

    # Status loop
    start = time.monotonic()
    try:
        while dm_ref.recording:
            elapsed = time.monotonic() - start
            remaining = max(0, duration - elapsed)
            mins = int(elapsed) // 60
            secs = int(elapsed) % 60

            parts = [
                f'🎥{dm_ref.cam_frames}',
                f'💓{dm_ref.oxi_samples}',
                f'📶{dm_ref.csi_packets}',
                f'⚡{dm_ref.emg_packets}',
                f'💧{dm_ref.gsr_samples}',
            ]
            status = ' | '.join(parts)
            print(f'\r  ⏱️ {mins:02d}:{secs:02d} / {duration}s  '
                  f'[{remaining:.0f}s left]  {status}   ',
                  end='', flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        print('\n\n  ⚠️  Interrupted!')
        dm_ref.stop_recording()

    # Summary
    print('\n')
    print('=' * 60)
    print('  ✅ Recording Complete!')
    print('=' * 60)
    print(f'  Session  : {dm_ref.session_id}')
    print(f'  Camera   : {dm_ref.cam_frames} frames')
    print(f'  Oximeter : {dm_ref.oxi_samples} samples')
    print(f'  CSI      : {dm_ref.csi_packets} packets')
    print(f'  EMG      : {dm_ref.emg_packets} packets')
    print(f'  GSR      : {dm_ref.gsr_samples} samples')
    print(f'  Output   : {dm_ref.session_dir}')
    print('=' * 60)

    # Data-quality report (written to quality_report.json by stop_recording)
    report = dm_ref.last_quality_report
    if report:
        badge = {'green': '🟢', 'yellow': '🟡', 'red': '🔴'}
        print(f'  Quality  : {badge.get(report["overall"], "?")} {report["overall"].upper()}')
        for name, d in report['sensors'].items():
            if d.get('status') == 'skipped':
                continue
            mark = badge.get(d['status'], '?')
            print(f'    {mark} {name:>9}: {d["rows_on_disk"]} rows '
                  f'({d.get("effective_hz", 0)} Hz) — {d["reason"]}')
        print('=' * 60)

    dm_ref.stop_monitoring()


# ─── Argument Parser ─────────────────────────────────────────────

def _build_parser():
    parser = argparse.ArgumentParser(
        description='Dataset Sync — Live Dashboard / Headless Recorder',
    )
    # Mode
    parser.add_argument('--headless', action='store_true',
                        help='Run without GUI (no Flask, no browser). '
                             'Saves RAM. Recording auto-starts.')
    parser.add_argument('--subject', type=str, default='unknown',
                        help='Subject ID for headless recording')
    parser.add_argument('--duration', type=int, default=60,
                        help='Recording duration in seconds (headless mode)')
    parser.add_argument('--force', action='store_true',
                        help='Bypass the readiness gate and record even if no '
                             'sensor is producing data (headless mode)')
    # Server
    parser.add_argument('--port', type=int, default=5000,
                        help='Web server port (GUI mode only)')
    # Sensors
    parser.add_argument('--camera-source', type=str, default='auto')
    parser.add_argument('--cam-res', type=str, default='1280x720')
    parser.add_argument('--record-format', type=str, default='video',
                        choices=['video', 'frames'])
    parser.add_argument('--oxi-port', type=str, default='auto')
    parser.add_argument('--csi-port', type=str, default='/dev/ttyUSB1')
    parser.add_argument('--csi-baud', type=int, default=115200)
    parser.add_argument('--emg-port', type=str, default='auto')
    parser.add_argument('--emg-baud', type=int, default=230400)
    parser.add_argument('--gsr-port', type=str, default='auto')
    parser.add_argument('--gsr-baud', type=int, default=115200)
    # Legacy
    parser.add_argument('--camera-id', type=int, default=None)
    return parser


# ─── Main ────────────────────────────────────────────────────────

def main():
    global dm, app, socketio
    args = _build_parser().parse_args()

    cam_source = args.camera_source
    if args.camera_id is not None:
        cam_source = args.camera_id

    try:
        rw, rh = args.cam_res.split('x')
        cam_res = (int(rw), int(rh))
    except ValueError:
        cam_res = (1920, 1080)

    if args.headless:
        # ── HEADLESS MODE ──
        # No Flask, no SocketIO, no MJPEG — minimal RAM
        sio = NullSIO()
        print()
        print('=' * 60)
        print('  📡 Dataset Sync — HEADLESS MODE')
        print('  ⚡ No GUI · No browser · Minimal RAM')
        print('=' * 60)
        print(f'  📹 Camera : {cam_source} @ {args.cam_res}')
        print(f'  📼 Format : {args.record_format}')
        print(f'  👤 Subject: {args.subject}')
        print(f'  ⏱️  Duration: {args.duration}s')
        print('=' * 60)
        print()

        dm = DeviceManager(
            sio=sio, camera_source=cam_source, cam_res=cam_res,
            record_format=args.record_format,
            oxi_port=args.oxi_port, csi_port=args.csi_port,
            csi_baud=args.csi_baud, emg_port=args.emg_port,
            emg_baud=args.emg_baud, gsr_port=args.gsr_port,
            gsr_baud=args.gsr_baud,
        )

        _run_headless(dm, args.subject, args.duration, force=args.force)
    else:
        # ── GUI MODE ──
        _init_flask()

        dm = DeviceManager(
            sio=socketio, camera_source=cam_source, cam_res=cam_res,
            record_format=args.record_format,
            oxi_port=args.oxi_port, csi_port=args.csi_port,
            csi_baud=args.csi_baud, emg_port=args.emg_port,
            emg_baud=args.emg_baud, gsr_port=args.gsr_port,
            gsr_baud=args.gsr_baud,
        )

        print()
        print('=' * 60)
        print('  📡 Dataset Sync — Live Dashboard')
        print(f'  📹 Camera: {cam_source} @ {args.cam_res}')
        print(f'  📼 Record format: {args.record_format}')
        print('=' * 60)
        print(f'  Open http://localhost:{args.port} in your browser')
        print('  💡 Use --headless to run without GUI (saves RAM)')
        print('=' * 60)
        print()

        try:
            socketio.run(app, host='0.0.0.0', port=args.port,
                         debug=False, allow_unsafe_werkzeug=True)
        finally:
            dm.stop_monitoring()


if __name__ == '__main__':
    main()
