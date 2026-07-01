"""
Sync Markers — Task ↔ Recording Alignment on the Master Clock
=============================================================
A session runs physiological recording (the master clock, `time.monotonic()` at
`rec_start`) alongside a stimulus/task protocol. Every task boundary and stimulus
event must be placed on that *same* master clock so the offline synchronizer can
align "what the subject was doing" with the physiology.

`SyncMarkerLog` is that bridge: the backend calls `.mark(label, ...)` and the
marker is stamped on the master clock **at receipt** and appended to
`<session_dir>/markers.csv`. Because the backend shares the recorder's process
(and thus its `time.monotonic()`), there is no clock offset to reconcile for
backend-origin events.

For events that originate on a *participant device* (e.g. an iPad browser), the
caller passes `t_device_s` (the device's own clock reading); we record both the
master-clock receipt time and the device time so the true offset/drift can be
estimated later (see `estimate_offset`).

Marker taxonomy (labels are free-form strings following this convention):
    session_start / session_end
    block_start:<id> / block_end:<id>
    stim_onset:<id> / stim_offset:<id>
    response:<block>:<trial>        payload: rt_ms, correct
    rating:<scale>:<id>             payload: valence/arousal/... or TLX
    flash:<n>                       the screen-flash sync event
    posture:<state>                 sit / stand
    cue:<name>                      breathing inhale/hold/exhale, buzzer, ...
    pause / resume / abort

Pure standard library.
"""

import csv
import json
import os
import time
from typing import Callable, List, Optional, Tuple

MARKER_COLUMNS = [
    't_master_s',   # master clock (monotonic - rec_start), stamped on receipt
    'label',        # taxonomy label
    'source',       # 'backend' | 'desktop' | 'ipad' | 'screen' | ...
    't_device_s',   # participant-device clock, if the event originated there
    'offset_s',     # t_master_s - t_device_s (blank unless t_device_s given)
    'payload_json', # arbitrary structured payload (rt_ms, correct, ratings, ...)
]


class SyncMarkerLog:
    """Append-only marker log stamped on the recorder's master clock."""

    def __init__(self, session_dir: str, t0: float,
                 source_default: str = 'backend',
                 clock: Optional[Callable[[], float]] = None):
        """
        Args:
            session_dir: session output directory (markers.csv is written here)
            t0: the recorder's rec_start (time.monotonic() at recording start)
            source_default: default `source` tag for marks
            clock: monotonic clock function (injectable for tests); default
                   time.monotonic
        """
        self.t0 = float(t0)
        self.source_default = source_default
        self._clock = clock or time.monotonic
        os.makedirs(session_dir, exist_ok=True)
        self._path = os.path.join(session_dir, 'markers.csv')
        is_new = not os.path.exists(self._path) or os.path.getsize(self._path) == 0
        self._f = open(self._path, 'a', newline='', buffering=1)  # line-buffered
        self._w = csv.writer(self._f)
        if is_new:
            self._w.writerow(MARKER_COLUMNS)
        self._closed = False

    # ── core ─────────────────────────────────────────────────────────────────

    def mark(self, label: str, source: Optional[str] = None,
             t_device_s: Optional[float] = None, **payload) -> dict:
        """
        Stamp an event on the master clock and append it. Returns the row dict.

        `t_master_s` is computed at *receipt* (now), which is the whole point —
        the recorder decides when the event happened on its own timeline, not the
        (possibly laggy) sender.
        """
        if self._closed:
            raise RuntimeError('SyncMarkerLog is closed')
        t_master = self._clock() - self.t0
        offset = (t_master - t_device_s) if t_device_s is not None else None
        row = {
            't_master_s': round(t_master, 6),
            'label': label,
            'source': source or self.source_default,
            't_device_s': ('' if t_device_s is None else round(t_device_s, 6)),
            'offset_s': ('' if offset is None else round(offset, 6)),
            'payload_json': (json.dumps(payload, separators=(',', ':'))
                             if payload else ''),
        }
        self._w.writerow([row[c] for c in MARKER_COLUMNS])
        return row

    def close(self):
        if not self._closed:
            try:
                self._f.flush()
                self._f.close()
            finally:
                self._closed = True

    # ── taxonomy convenience wrappers ─────────────────────────────────────────

    def session_start(self, **payload):      return self.mark('session_start', **payload)
    def session_end(self, **payload):        return self.mark('session_end', **payload)
    def block_start(self, block_id, **p):    return self.mark(f'block_start:{block_id}', **p)
    def block_end(self, block_id, **p):      return self.mark(f'block_end:{block_id}', **p)
    def stim_onset(self, stim_id, **p):      return self.mark(f'stim_onset:{stim_id}', **p)
    def stim_offset(self, stim_id, **p):     return self.mark(f'stim_offset:{stim_id}', **p)

    def response(self, block_id, trial, rt_ms=None, correct=None, **p):
        return self.mark(f'response:{block_id}:{trial}',
                         rt_ms=rt_ms, correct=correct, **p)

    def rating(self, scale, stim_id, **values):
        return self.mark(f'rating:{scale}:{stim_id}', **values)

    def flash(self, n, **p):                 return self.mark(f'flash:{n}', source='screen', **p)
    def posture(self, state, **p):           return self.mark(f'posture:{state}', **p)
    def cue(self, name, **p):                return self.mark(f'cue:{name}', **p)
    def pause(self, **p):                    return self.mark('pause', **p)
    def resume(self, **p):                   return self.mark('resume', **p)
    def abort(self, **p):                    return self.mark('abort', **p)


# ─── Device-clock offset / drift estimation ──────────────────────────────────

def estimate_offset(pairs: List[Tuple[float, float]]) -> Optional[dict]:
    """
    Given (t_master, t_device) pairs from device-origin markers, fit
    t_master = a*t_device + b. Returns {offset_s (=b), drift_ppm (=(a-1)*1e6),
    residual_ms, n}. Use this to map a participant device's clock onto the master
    clock. Returns None if fewer than 2 points.
    """
    n = len(pairs)
    if n < 2:
        return None
    xs = [d for _, d in pairs]
    ys = [m for m, _ in pairs]
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        # no spread in device time — fall back to a pure median offset
        offs = sorted(m - d for m, d in pairs)
        return {'offset_s': round(offs[n // 2], 6), 'drift_ppm': 0.0,
                'residual_ms': 0.0, 'n': n}
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    resid = [(y - (a * x + b)) for x, y in zip(xs, ys)]
    mean_r = sum(resid) / n
    var = sum((r - mean_r) ** 2 for r in resid) / n
    return {
        'offset_s': round(b, 6),
        'drift_ppm': round((a - 1.0) * 1e6, 1),
        'residual_ms': round(var ** 0.5 * 1000.0, 3),
        'n': n,
    }
