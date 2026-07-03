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
from src.dashboard.handlers.thermal_handler import ThermalHandler
from src.recorder.ipc_server import IpcSIO
from src.recorder.sync_markers import SyncMarkerLog

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
                 gsr_port='auto', gsr_baud=115200,
                 thermal_source='none', thermal_res=(256, 192),
                 audio=False, audio_kwargs=None, audio_device=None,
                 ecg=False, ecg_address=None, marker_port=None):

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
        self.thermal = ThermalHandler(self.registry, self.sio,
                                      source=thermal_source, resolution=thermal_res)

        # Audio is a first-class captured modality the daemon owns directly, so the
        # whole recording path has a single authoritative clock owner (brief §1).
        # `captures_audio` lets an embedding RecorderBridge defer to us instead of
        # starting a second, competing microphone stream.
        self.captures_audio = bool(audio)
        self._audio_kwargs = dict(audio_kwargs or {})
        if audio_device is not None:
            self._audio_kwargs.setdefault('device', audio_device)
        self.audio = None

        # ECG ground truth is a Polar H10 over BLE (not serial), owned here too so
        # its samples anchor to the same rec_start origin.
        self.captures_ecg = bool(ecg)
        self._ecg_address = ecg_address
        self.ecg = None
        self._ecg_stats = None

        # The daemon owns the marker log so markers exist in "recording + external
        # task app" mode (no session runner required). An optional HTTP ingest lets
        # a separate task interface POST events that land on this master clock.
        self.markers = None
        self._marker_port = marker_port
        self._ingest = None

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

    def mark(self, label, source='task', t_device_s=None, **payload):
        """Stamp an event on the master clock. Returns the row dict, or None when
        no session is active (the ingest server maps None → HTTP 409). This is the
        single surface both the session runner and the external task app use."""
        if self.markers is None:
            return None
        return self.markers.mark(label, source=source, t_device_s=t_device_s,
                                 **payload)

    def master_time(self):
        """Current master-clock time (monotonic - rec_start), or None if idle."""
        return (time.monotonic() - self.rec_start) if self.rec_start else None

    # Marker convenience + health, so the OperatorConsole can drive the daemon
    # directly (same surface as RecorderBridge).
    def pause(self, **p):  return self.mark('pause', source='operator', **p)
    def resume(self, **p): return self.mark('resume', source='operator', **p)
    def abort(self, **p):  return self.mark('abort', source='operator', **p)

    def get_health(self) -> dict:
        """Per-sensor liveness snapshot for the operator console."""
        out = {'recording': self.recording, 'session_id': self.session_id,
               'sensors': {}}
        reg = self.registry
        for name in ['camera', 'oximeter', 'csi', 'emg', 'gsr', 'thermal']:
            try:
                info = reg.get_sensor(name)
            except Exception:
                info = None
            if info is None:
                continue
            out['sensors'][name] = {
                'state': getattr(getattr(info, 'state', None), 'value', str(info)),
                'ok': reg.is_ok(name) if hasattr(reg, 'is_ok') else None,
                'count': reg.get_counter(name) if hasattr(reg, 'get_counter') else None,
            }
        return out

    def start_monitoring(self):
        """Start all sensor threads. Oximeter gets priority."""
        self._stop.clear()
        self.sio.start()  # Start IPC state broadcaster

        # Optional marker ingest for a separate task interface (lives for the whole
        # daemon session; mark() guards on there being an active recording).
        if self._marker_port is not None and self._ingest is None:
            try:
                from src.recorder.marker_ingest import MarkerIngestServer
                self._ingest = MarkerIngestServer(
                    self.mark, port=self._marker_port,
                    health_fn=lambda: {'recording': self.recording,
                                       'session_id': self.session_id},
                    master_clock_fn=self.master_time).start()
                print(f'  🛰  marker ingest listening on :{self._ingest.port} '
                      f'(POST /mark)')
            except Exception as e:
                print(f'  ⚠ marker ingest unavailable: {str(e)[:120]}')

        # Define handlers: (name, handler, retries, delay, high_priority)
        handlers = [
            ('camera',   self.camera,   3, 5,  False),
            ('csi',      self.csi,      3, 5,  False),
            ('oximeter', self.oximeter, 10, 5, True),   # More retries, high priority
            ('emg',      self.emg,      5, 8,  False),
            ('gsr',      self.gsr,      5, 8,  False),
            ('thermal',  self.thermal,  3, 5,  False),  # source='none' → disabled
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
        if self._ingest is not None:
            self._ingest.stop()
            self._ingest = None
        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr, self.thermal]:
            handler.stop()
        self.sio.stop()

    def start_recording(self, subject, duration):
        """Start a synchronized recording session."""
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        ts = now.strftime("%Y%m%d_%H%M%S")
        self.session_id = f"session_{ts}"
        self.session_dir = os.path.join(BASE_DIR, 'data', 'raw', self.session_id)

        for sub in ['camera', 'oximeter', 'csi', 'emg', 'gsr', 'thermal',
                    'audio', 'ecg']:
            os.makedirs(os.path.join(self.session_dir, sub), exist_ok=True)

        self.rec_start = time.monotonic()
        self.registry.reset_all_counters()
        self.recording = True

        for handler in [self.camera, self.oximeter, self.csi,
                        self.emg, self.gsr, self.thermal]:
            # Set the timestamp origin and output dir BEFORE flipping `recording`
            # on, so a handler thread can never observe recording==True while
            # rec_start is still None (would compute monotonic() - None).
            handler.session_dir = self.session_dir
            handler.rec_start = self.rec_start
            handler.recording = True

        # Master-clock marker log, owned by the daemon. Both the in-process session
        # runner and any external task app (via the ingest server) write here.
        self.markers = SyncMarkerLog(self.session_dir, self.rec_start)
        self.markers.session_start(subject=subject, duration=duration,
                                   session_id=self.session_id)

        # Audio: recorder-owned mic stream anchored to the SAME rec_start origin,
        # so its t_start_master_s lands on the master clock. Degrades cleanly if
        # sounddevice/mic are unavailable (never fabricates silence).
        self.audio = None
        if self.captures_audio:
            try:
                from src.recorder.audio_recorder import AudioRecorder
                self.audio = AudioRecorder(
                    os.path.join(self.session_dir, 'audio'), self.rec_start,
                    **self._audio_kwargs)
                self.audio.start()
            except Exception as e:
                print(f'  ⚠ audio unavailable: {str(e)[:120]}')
                self.audio = None

        # ECG (Polar H10 BLE) — degrades cleanly if bleak/strap are unavailable.
        self.ecg = None
        if self.captures_ecg:
            try:
                from src.recorder.ecg_recorder import PolarH10ECGRecorder
                self.ecg = PolarH10ECGRecorder(
                    os.path.join(self.session_dir, 'ecg'), self.rec_start,
                    address=self._ecg_address)
                self.ecg.start()
            except Exception as e:
                print(f'  ⚠ ECG unavailable: {str(e)[:120]}')
                self.ecg = None

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
                        self.emg, self.gsr, self.thermal]:
            handler.recording = False
        self.camera.stop_recording_files()
        self.thermal.stop_recording_files()
        if self.audio is not None:
            try:
                self.audio.stop()          # writes audio.wav + audio_meta.json
            except Exception:
                pass
            self.audio = None
        if self.ecg is not None:
            try:
                self._ecg_stats = self.ecg.stop()   # flushes ecg_log.csv
            except Exception:
                self._ecg_stats = None
            self.ecg = None
        if self.markers is not None:
            try:
                self.markers.session_end()
                self.markers.close()
            except Exception:
                pass
            self.markers = None
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
                    'thermal': self.registry.get_counter('thermal'),
                    'ecg': (self._ecg_stats or {}).get('n_samples', 0),
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
    parser.add_argument('--thermal-source', type=str, default='none',
                        help="Thermal camera V4L2 node (e.g. /dev/video2, or "
                             "'auto'); 'none' disables it")
    parser.add_argument('--audio', action='store_true',
                        help='Capture recorder-owned audio on the master clock')
    parser.add_argument('--audio-device', default=None,
                        help="Audio input: device index/name, or 'hdmi' to auto-pick "
                             "the capture card (a6000 audio-over-HDMI, same clock as video)")
    parser.add_argument('--list-audio-devices', action='store_true',
                        help='List audio input devices and exit')
    parser.add_argument('--ecg', action='store_true',
                        help='Capture Polar H10 ECG over BLE on the master clock')
    parser.add_argument('--ecg-address', type=str, default=None,
                        help='Polar H10 BLE address (default: scan by name)')
    parser.add_argument('--marker-port', type=int, default=None,
                        help='Start the HTTP marker-ingest server on this port so a '
                             'separate task app can POST /mark events on the master clock')
    parser.add_argument('--console', action='store_true',
                        help='Live operator console (health board + [a]bort/[p]ause keys) '
                             'instead of the plain status line')

    args = parser.parse_args()

    if args.list_audio_devices:
        from src.recorder.audio_recorder import list_input_devices
        print('Audio input devices:')
        for i, name, ch in list_input_devices():
            print(f'  [{i}] {name}  ({ch} ch)')
        return

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
        thermal_source=args.thermal_source, audio=args.audio,
        audio_device=args.audio_device,
        ecg=args.ecg, ecg_address=args.ecg_address,
        marker_port=args.marker_port,
    )

    daemon.start_monitoring()

    print('  ⏳ Waiting 5s for sensors to connect...')
    time.sleep(5)

    # Print sensor status
    print()
    for sensor in ['oximeter', 'camera', 'csi', 'emg', 'gsr', 'thermal']:
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

    # Operator console: live health board + [a]bort/[p]ause keys (hands-off mode,
    # e.g. daemon + external task app). Skips the plain status line.
    if args.console:
        from src.session.operator_console import OperatorConsole
        from src.session.runner import SessionControls
        controls = SessionControls()
        console = OperatorConsole(daemon, controls,
                                  min_hz={'oximeter': 20, 'camera': 15})
        console.start()
        try:
            while daemon.recording and not controls.stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            controls.abort()
        console.stop()
        if daemon.recording:
            daemon.stop_recording()
        _print_summary(daemon)
        daemon.stop_monitoring()
        return

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

    _print_summary(daemon)
    daemon.stop_monitoring()


def _print_summary(daemon):
    r = daemon.registry
    print('\n')
    print('=' * 60)
    print('  ✅ Recording Complete!')
    print('=' * 60)
    print(f'  Session  : {daemon.session_id}')
    print(f'  💓 Oximeter : {r.get_counter("oximeter")} samples')
    print(f'  🎥 Camera   : {r.get_counter("camera")} frames')
    print(f'  📶 CSI      : {r.get_counter("csi")} packets')
    print(f'  ⚡ EMG      : {r.get_counter("emg")} packets')
    print(f'  💧 GSR      : {r.get_counter("gsr")} samples')
    print(f'  🌡️  Thermal  : {r.get_counter("thermal")} frames')
    print(f'  📁 Output   : {daemon.session_dir}')
    print('=' * 60)


if __name__ == '__main__':
    main()
