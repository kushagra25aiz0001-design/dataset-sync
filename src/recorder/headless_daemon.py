"""
Headless Recording Daemon
=========================
Independent background recording logic. Runs without Flask or SocketIO
to guarantee zero GUI overhead and prevent crashes.

The oximeter thread runs at elevated priority since rPPG synchronization
depends entirely on accurate, uninterrupted heart rate data.

Usage:
    python -m src.recorder.headless_daemon --subject test01 --duration 120
"""

import argparse
import json
import os
import sys
import time
import threading
from datetime import datetime, timezone, timedelta

from src.recorder.sensor_registry import SensorRegistry, SensorState
from src.recorder.sensor_orchestrator import SensorOrchestrator
from src.dashboard.handlers.camera_handler import CameraHandler
from src.dashboard.handlers.oximeter_handler import OximeterHandler
from src.dashboard.handlers.csi_handler import CSIHandler
from src.dashboard.handlers.emg_handler import EMGHandler
from src.dashboard.handlers.gsr_handler import GSRHandler
from src.recorder.ipc_server import IpcSIO

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _set_high_priority():
    """
    Attempt to raise the current thread's scheduling priority.
    This ensures the oximeter serial reader is never starved by
    camera encoding, CSI parsing, or other CPU-heavy threads.
    """
    try:
        os.nice(-10)  # Lower nice = higher priority
    except (PermissionError, OSError):
        pass

    # Try POSIX real-time scheduling (needs root or CAP_SYS_NICE)
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)

        SCHED_RR = 2
        class sched_param(ctypes.Structure):
            _fields_ = [('sched_priority', ctypes.c_int)]

        param = sched_param(sched_priority=20)
        tid = libc.syscall(186)  # SYS_gettid on Linux
        result = libc.sched_setscheduler(tid, SCHED_RR, ctypes.byref(param))
        if result == 0:
            return 'SCHED_RR(20)'
    except Exception:
        pass

    return 'nice(-10)'


class HeadlessDaemon:
    """
    Headless recording daemon with oximeter-first priority.

    Architecture:
        - No Flask, no SocketIO, no MJPEG encoding
        - IpcSIO silently discards emit() calls (zero overhead)
        - Oximeter thread runs at elevated OS priority
        - CSV writes use append mode (reconnects don't lose data)
        - Terminal prints live status every second
    """

    def __init__(self, camera_source='auto', cam_res=(1920, 1080),
                 record_format='video', oxi_port='auto',
                 csi_port='/dev/ttyUSB1', csi_baud=115200,
                 emg_port='auto', emg_baud=230400,
                 gsr_port='auto', gsr_baud=115200):

        self.registry = SensorRegistry()
        self.record_format = record_format

        # IpcSIO: silent drop-in for SocketIO — zero RAM overhead
        self.sio = IpcSIO(self.registry)

        self.orchestrator = SensorOrchestrator(self.registry, self.sio)

        # Initialize handlers
        self.camera = CameraHandler(
            self.registry, self.sio, source=camera_source,
            resolution=cam_res, record_format=record_format,
        )
        self.oximeter = OximeterHandler(self.registry, self.sio, port_cfg=oxi_port)
        self.csi = CSIHandler(self.registry, self.sio, port=csi_port, baud=csi_baud)
        self.emg = EMGHandler(self.registry, self.sio, port=emg_port, baud=emg_baud)
        self.gsr = GSRHandler(self.registry, self.sio, port=gsr_port, baud=gsr_baud)

        self.recording = False
        self.session_dir = None
        self.session_id = None
        self.rec_start = None
        self._stop = threading.Event()

    def _sensor_retry_wrapper(self, device_name, handler,
                              max_retries=5, retry_delay=8,
                              high_priority=False):
        """Run a sensor handler with retry logic and optional priority boost."""
        if high_priority:
            prio = _set_high_priority()
            print(f'  ⚡ Oximeter thread priority: {prio}')

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
                print(f'  ❌ {device_name}: {msg}')
                if attempt < max_retries:
                    time.sleep(retry_delay)

        if attempt >= max_retries and not self._stop.is_set():
            self.registry.set_state(
                device_name, SensorState.ERROR,
                f'Gave up after {max_retries} attempts',
            )

    def start_monitoring(self):
        """Start all sensor threads. Oximeter gets priority."""
        self._stop.clear()
        self.sio.start()  # Start IPC state broadcaster

        # Define handlers: (name, handler, retries, delay, high_priority)
        handlers = [
            ('camera',   self.camera,   3, 5,  False),
            ('csi',      self.csi,      3, 5,  False),
            ('oximeter', self.oximeter, 10, 5, True),   # More retries, high priority
            ('emg',      self.emg,      5, 8,  False),
            ('gsr',      self.gsr,      5, 8,  False),
        ]

        for name, handler, retries, delay, high_prio in handlers:
            port_cfg = (getattr(handler, 'port_cfg', None)
                        or getattr(handler, 'port', None)
                        or getattr(handler, 'source', None))
            if port_cfg == 'none':
                self.registry.set_state(name, SensorState.DISABLED, 'Disabled')
                continue
            t = threading.Thread(
                target=self._sensor_retry_wrapper,
                args=(name, handler, retries, delay, high_prio),
                daemon=True, name=name,
            )
            t.start()

    def stop_monitoring(self):
        """Stop all sensor threads."""
        self._stop.set()
        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr]:
            handler.stop()
        self.sio.stop()

    def start_recording(self, subject, duration):
        """Start a synchronized recording session."""
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        ts = now.strftime("%Y%m%d_%H%M%S")
        self.session_id = f"session_{ts}"
        self.session_dir = os.path.join(BASE_DIR, 'data', 'raw', self.session_id)

        for sub in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
            os.makedirs(os.path.join(self.session_dir, sub), exist_ok=True)

        self.rec_start = time.monotonic()
        self.registry.reset_all_counters()
        self.recording = True

        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr]:
            # Set the timestamp origin and output dir BEFORE flipping `recording`
            # on, so a handler thread can never observe recording==True while
            # rec_start is still None (would compute monotonic() - None).
            handler.session_dir = self.session_dir
            handler.rec_start = self.rec_start
            handler.recording = True

        # Save metadata
        meta = {
            'session_id': self.session_id,
            'subject': subject,
            'start': now.isoformat(),
            'duration_target': duration,
            'camera': self.camera.cam_info.copy(),
            'record_format': self.record_format,
        }
        with open(os.path.join(self.session_dir, 'metadata.json'), 'w') as f:
            json.dump(meta, f, indent=2)

        self.sio.set_recording_state(True, self.session_id, duration,
                                     self.rec_start)

        # Auto-stop timer
        def _auto_stop():
            time.sleep(duration)
            if self.recording:
                self.stop_recording()
        threading.Thread(target=_auto_stop, daemon=True).start()
        return self.session_id

    def stop_recording(self):
        """Stop recording and finalize metadata."""
        self.recording = False
        self.sio.set_recording_state(False)
        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr]:
            handler.recording = False
        self.camera.stop_recording_files()
        time.sleep(0.3)

        if self.session_dir:
            meta_path = os.path.join(self.session_dir, 'metadata.json')
            if os.path.exists(meta_path):
                with open(meta_path) as f:
                    meta = json.load(f)
                meta['duration_actual'] = round(
                    time.monotonic() - self.rec_start, 2,
                ) if self.rec_start else 0
                meta['stats'] = {
                    'cam': self.registry.get_counter('camera'),
                    'oxi': self.registry.get_counter('oximeter'),
                    'csi': self.registry.get_counter('csi'),
                    'emg': self.registry.get_counter('emg'),
                    'gsr': self.registry.get_counter('gsr'),
                }
                with open(meta_path, 'w') as f:
                    json.dump(meta, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description='Headless Recording Daemon (oximeter-priority)',
    )
    parser.add_argument('--subject', type=str, default='unknown',
                        help='Subject ID for the recording session')
    parser.add_argument('--duration', type=int, default=60,
                        help='Recording duration in seconds')
    parser.add_argument('--camera-source', type=str, default='auto')
    parser.add_argument('--cam-res', type=str, default='1280x720')
    parser.add_argument('--record-format', type=str, default='video')
    parser.add_argument('--oxi-port', type=str, default='auto')
    parser.add_argument('--csi-port', type=str, default='/dev/ttyUSB1')
    parser.add_argument('--csi-baud', type=int, default=115200)
    parser.add_argument('--emg-port', type=str, default='auto')
    parser.add_argument('--emg-baud', type=int, default=230400)
    parser.add_argument('--gsr-port', type=str, default='auto')
    parser.add_argument('--gsr-baud', type=int, default=115200)

    args = parser.parse_args()

    try:
        rw, rh = args.cam_res.split('x')
        cam_res = (int(rw), int(rh))
    except ValueError:
        cam_res = (1920, 1080)

    print()
    print('=' * 60)
    print('  📡 Dataset Sync — HEADLESS DAEMON')
    print('  ⚡ No GUI · Zero socket overhead · Oximeter priority')
    print('=' * 60)
    print(f'  👤 Subject : {args.subject}')
    print(f'  ⏱️  Duration: {args.duration}s')
    print(f'  📹 Camera  : {args.camera_source}')
    print(f'  💓 Oximeter: {args.oxi_port}')
    print('=' * 60)
    print()

    daemon = HeadlessDaemon(
        camera_source=args.camera_source, cam_res=cam_res,
        record_format=args.record_format,
        oxi_port=args.oxi_port, csi_port=args.csi_port,
        csi_baud=args.csi_baud, emg_port=args.emg_port,
        emg_baud=args.emg_baud, gsr_port=args.gsr_port,
        gsr_baud=args.gsr_baud,
    )

    daemon.start_monitoring()

    print('  ⏳ Waiting 5s for sensors to connect...')
    time.sleep(5)

    # Print sensor status
    print()
    for sensor in ['oximeter', 'camera', 'csi', 'emg', 'gsr']:
        info = daemon.registry.get_sensor(sensor)
        if info and info.state == SensorState.DISABLED:
            icon = '⬛'
        elif daemon.registry.is_ok(sensor):
            icon = '✅'
        else:
            icon = '❌'
        msg = info.status_msg if info else 'Unknown'
        print(f'  {icon} {sensor:>10}: {msg}')
    print()

    # Start recording
    sid = daemon.start_recording(args.subject, args.duration)
    print(f'  🔴 RECORDING: {sid}')
    print(f'  📁 Output: {daemon.session_dir}')
    print()

    # Live terminal status — update every second
    start = time.monotonic()
    last_oxi = 0
    try:
        while daemon.recording:
            elapsed = time.monotonic() - start
            mins = int(elapsed) // 60
            secs = int(elapsed) % 60
            remaining = max(0, args.duration - elapsed)

            oxi_now = daemon.registry.get_counter('oximeter')
            oxi_rate = oxi_now - last_oxi  # samples/sec
            last_oxi = oxi_now

            # Progress bar
            progress_val = min(1.0, elapsed / args.duration)
            bar_len = 15
            filled = int(bar_len * progress_val)
            bar = '█' * filled + '░' * (bar_len - filled)
            percent = int(progress_val * 100)

            # Get latest values from IPC cache
            oxi_data = daemon.sio._latest_oxi or {}
            emg_data = daemon.sio._latest_emg or {}
            gsr_data = daemon.sio._latest_gsr or {}

            spo2 = oxi_data.get('spo2', 0)
            hr = oxi_data.get('hr', 0)
            emg_v = emg_data.get('voltage', 0.0)
            gsr_r = gsr_data.get('resistance', 0.0)

            # Format real-time string
            val_str = (
                f"SpO2:{spo2:>3}% HR:{hr:>3} | "
                f"EMG:{emg_v:>5.1f}mV | GSR:{gsr_r:>5.1f}kΩ"
            )

            # Show counters & rate
            parts = [
                f'💓OXI:{oxi_rate}/s',
                f'🎥CAM:{daemon.registry.get_counter("camera")}',
                f'📶CSI:{daemon.registry.get_counter("csi")}',
            ]
            status = ' '.join(parts)

            print(f'\r  [{bar}] {percent:>3}% | {mins:02d}:{secs:02d} | {val_str} | {status}   ', end='', flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n\n  ⚠️  Interrupted by user')
        daemon.stop_recording()

    # Final summary
    print('\n')
    print('=' * 60)
    print('  ✅ Recording Complete!')
    print('=' * 60)
    print(f'  Session  : {daemon.session_id}')
    print(f'  💓 Oximeter : {daemon.registry.get_counter("oximeter")} samples')
    print(f'  🎥 Camera   : {daemon.registry.get_counter("camera")} frames')
    print(f'  📶 CSI      : {daemon.registry.get_counter("csi")} packets')
    print(f'  ⚡ EMG      : {daemon.registry.get_counter("emg")} packets')
    print(f'  💧 GSR      : {daemon.registry.get_counter("gsr")} samples')
    print(f'  📁 Output   : {daemon.session_dir}')
    print('=' * 60)

    daemon.stop_monitoring()


if __name__ == '__main__':
    main()
