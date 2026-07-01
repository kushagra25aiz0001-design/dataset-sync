"""
Recorder Bridge — In-Process Control + Marker Surface for the Session Runner
===========================================================================
The session-runner backend drives a protocol (blocks, stimuli, ratings) while
the physiological recorder runs. This bridge is the single object the backend
talks to. It **embeds the recorder in the same process**, so:

    - start/stop control is a direct method call (no IPC latency), and
    - every marker is stamped on the recorder's own `time.monotonic()` origin —
      there is literally no clock offset to reconcile for backend-origin events.

The bridge is recorder-agnostic: it needs an object exposing
    start_recording(subject, duration) -> session_id
    stop_recording()
    .rec_start   (monotonic origin, set by start_recording)
    .session_dir
    .registry    (SensorRegistry, for health)  [optional]
The headless daemon satisfies this; `embed_headless_daemon()` wires it up (with a
lazy import so this module loads without OpenCV/pyserial for testing).

All task-boundary/stimulus events go through `mark()` or its taxonomy wrappers,
which delegate to `SyncMarkerLog` (writes markers.csv on the master clock).
"""

import time
from typing import Optional

from src.recorder.sync_markers import SyncMarkerLog


class RecorderBridge:
    def __init__(self, recorder):
        self.recorder = recorder
        self.markers: Optional[SyncMarkerLog] = None
        self.recording = False
        self.session_id: Optional[str] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self, subject: str, duration: int) -> dict:
        """
        Start recording and open the marker log on the recorder's master clock.
        Returns {session_id, t0_monotonic} so the caller can relate its own
        timing to the master clock if needed.
        """
        if self.recording:
            raise RuntimeError('already recording')
        sid = self.recorder.start_recording(subject, duration)
        if sid is None:
            # recorder refused (e.g. readiness/disk gate)
            return {'session_id': None, 't0_monotonic': None, 'ok': False}
        t0 = self.recorder.rec_start
        self.markers = SyncMarkerLog(self.recorder.session_dir, t0)
        self.recording = True
        self.session_id = sid
        self.markers.session_start(subject=subject, duration=duration,
                                   session_id=sid)
        return {'session_id': sid, 't0_monotonic': t0, 'ok': True}

    def stop(self) -> dict:
        """Mark session_end, close the marker log, and stop the recorder."""
        if self.markers is not None:
            self.markers.session_end()
            self.markers.close()
            self.markers = None
        self.recording = False
        try:
            self.recorder.stop_recording()
        except Exception:
            pass
        sid, self.session_id = self.session_id, None
        return {'session_id': sid}

    # ── marking (guarded: only between start and stop) ────────────────────────

    def mark(self, label: str, source: Optional[str] = None,
             t_device_s: Optional[float] = None, **payload) -> Optional[dict]:
        """
        Stamp an event on the master clock. Returns None (ignored) if called
        outside a recording session — the app can fire markers freely without
        guarding on recording state itself.
        """
        if self.markers is None:
            return None
        return self.markers.mark(label, source=source,
                                 t_device_s=t_device_s, **payload)

    # taxonomy passthroughs (mirror SyncMarkerLog)
    def block_start(self, block_id, **p): return self.mark(f'block_start:{block_id}', **p)
    def block_end(self, block_id, **p):   return self.mark(f'block_end:{block_id}', **p)
    def stim_onset(self, stim_id, **p):   return self.mark(f'stim_onset:{stim_id}', **p)
    def stim_offset(self, stim_id, **p):  return self.mark(f'stim_offset:{stim_id}', **p)

    def response(self, block_id, trial, rt_ms=None, correct=None, **p):
        return self.mark(f'response:{block_id}:{trial}',
                         rt_ms=rt_ms, correct=correct, **p)

    def rating(self, scale, stim_id, **values):
        return self.mark(f'rating:{scale}:{stim_id}', **values)

    def flash(self, n, **p):     return self.mark(f'flash:{n}', source='screen', **p)
    def posture(self, state, **p): return self.mark(f'posture:{state}', **p)
    def cue(self, name, **p):    return self.mark(f'cue:{name}', **p)
    def pause(self, **p):        return self.mark('pause', **p)
    def resume(self, **p):       return self.mark('resume', **p)
    def abort(self, **p):        return self.mark('abort', **p)

    # ── health (for the operator console) ─────────────────────────────────────

    def get_health(self) -> dict:
        """
        Per-sensor liveness snapshot for the operator console: state, ok flag,
        and sample counter. Safe to call any time.
        """
        reg = getattr(self.recorder, 'registry', None)
        out = {'recording': self.recording, 'session_id': self.session_id,
               'sensors': {}}
        if reg is None:
            return out
        for name in ['camera', 'oximeter', 'csi', 'emg', 'gsr']:
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
                'status': getattr(info, 'status_msg', ''),
            }
        return out

    # ── in-process embedding of the authoritative recorder ────────────────────

    @classmethod
    def embed_headless_daemon(cls, start_monitoring: bool = True,
                              warmup_s: float = 0.0, **daemon_kwargs):
        """
        Construct + wrap the headless daemon (the authoritative 5-modality,
        monotonic-clock recorder). Heavy deps (OpenCV/pyserial) are imported
        here, lazily, so this module stays import-light for testing.

        Returns (bridge, daemon).
        """
        from src.recorder.headless_daemon import HeadlessDaemon
        daemon = HeadlessDaemon(**daemon_kwargs)
        if start_monitoring:
            daemon.start_monitoring()
            if warmup_s > 0:
                time.sleep(warmup_s)
        return cls(daemon), daemon
