"""
Post-hoc ECG alignment — put a self-recording ECG on the master clock
=====================================================================
Some ECG devices (Frontier X, Apple Watch, most Holter monitors) do NOT stream
live — they record to their own storage on their own RTC and you **export the
waveform afterward**. That export is on the *device* clock, which is unrelated to
our master clock (``time.monotonic() - rec_start``). Wall-clock timestamps only
get you ~1 s alignment (phone↔PC skew + coarse app timestamps), which is useless
for beat-accurate work.

The fix: **the heartbeat is a shared clock.** We already record the oximeter PPG on
the master clock, and it sees the *same heart* as the ECG. The two beat-interval
(HRV) sequences are an identical fingerprint, so cross-correlating them recovers
the constant offset, and a least-squares refit on the matched beats recovers the
slow device-vs-PC **drift**. Result: ``t_master = a·t_ecg_device + b``.

Pipeline
--------
    ecg_beats  (R-peak times on the Frontier X clock)     ─┐
    ppg_beats  (pulse-peak times on the master clock)     ─┤─▶ align_beats()
                                                            └─▶ {offset_s, drift_ppm,
                                                                  residual_ms, n_matched, ...}
    then: master_ecg_times = remap_times(ecg_sample_times, a, b)  → ecg/ecg_log.csv

This module is the format-independent **engine** (pure standard library). The
Frontier-X-specific export parser + R-peak/pulse-peak detection plug in on top
once the export format is known. Seeding with a coarse prior (e.g. a deliberate
chest-tap logged as a master-clock marker) makes the lock unambiguous even for
very steady heart rates.
"""

import glob
import json
import os
from datetime import datetime
from typing import List, Optional, Sequence

from src.recorder.sync_markers import estimate_offset


# ── Coarse wall-clock alignment (works from just the report's Start Time) ──────
# The Frontier X *report* gives an absolute Start Time (local wall clock). Our
# recorder's metadata.json gives the session's wall-clock `start`. Both are the
# same local clock, so subtracting them places the ECG on the master clock to
# ~1 s (phone-vs-PC skew + 1 s label resolution). That's enough for block-level
# work AND serves as the `prior_offset_s` seed for the beat-accurate step.

def parse_wallclock(s: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM:SS' (report) or ISO-8601 w/ tz (metadata) as a
    naive local datetime (tz dropped — both sides are the same local clock)."""
    s = s.strip()
    dt = datetime.fromisoformat(s.replace(' ', 'T'))
    return dt.replace(tzinfo=None)


def map_wall_to_master(wall: str, session_start: str) -> float:
    """Master-clock time (s since rec_start) of a wall-clock instant."""
    return (parse_wallclock(wall) - parse_wallclock(session_start)).total_seconds()


def match_ecg_to_session(ecg_start: str, ecg_end: str,
                         raw_root: str = 'data/raw',
                         max_skew_s: float = 60.0) -> Optional[dict]:
    """Find the recording session whose window best overlaps this ECG report.
    Returns {session_id, session_start, offset_s (master time the ECG began),
    overlap_s, overlap_window} for the best match, or None."""
    es = parse_wallclock(ecg_start)
    ee = parse_wallclock(ecg_end)
    best = None
    for meta_path in sorted(glob.glob(os.path.join(raw_root, 'session_*', 'metadata.json'))):
        try:
            m = json.load(open(meta_path))
            ss = parse_wallclock(m['start'])
        except (ValueError, KeyError, OSError):
            continue
        dur = float(m.get('duration_target') or m.get('duration_actual') or 0)
        # ECG and session windows on the master clock (session start = 0).
        ecg_lo = (es - ss).total_seconds()
        ecg_hi = (ee - ss).total_seconds()
        ov = min(ecg_hi, dur) - max(ecg_lo, 0.0)
        if ov <= 0 or abs(ecg_lo) > max_skew_s:
            continue
        if best is None or ov > best['overlap_s']:
            best = {'session_id': m.get('session_id'),
                    'session_start': m['start'],
                    'offset_s': round(ecg_lo, 3),
                    'overlap_s': round(ov, 1),
                    'overlap_window': [round(max(ecg_lo, 0.0), 1),
                                       round(min(ecg_hi, dur), 1)]}
    return best


# ── beat series → instantaneous rate ─────────────────────────────────────────

def instantaneous_rate(beats: Sequence[float]) -> "tuple[List[float], List[float]]":
    """(times, bpm): instantaneous heart rate sampled at each beat (except last).
    bpm_i = 60 / (beats[i+1] - beats[i]), located at beats[i]."""
    beats = sorted(beats)
    times, bpm = [], []
    for i in range(len(beats) - 1):
        dt = beats[i + 1] - beats[i]
        if dt > 0:
            times.append(beats[i])
            bpm.append(60.0 / dt)
    return times, bpm


def _interp(grid: List[float], xs: List[float], ys: List[float]) -> List[Optional[float]]:
    """Linear interp of (xs, ys) onto grid; None outside [xs[0], xs[-1]]."""
    n = len(xs)
    out: List[Optional[float]] = [None] * len(grid)
    if n == 0:
        return out
    j = 0
    for i, g in enumerate(grid):
        if g < xs[0] or g > xs[-1]:
            continue
        while j < n - 2 and xs[j + 1] < g:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        y0, y1 = ys[j], ys[j + 1]
        out[i] = y0 if x1 == x0 else y0 + (y1 - y0) * (g - x0) / (x1 - x0)
    return out


def _zscore(vals: List[Optional[float]]):
    present = [v for v in vals if v is not None]
    if len(present) < 2:
        return None, None
    m = sum(present) / len(present)
    var = sum((v - m) ** 2 for v in present) / len(present)
    sd = var ** 0.5
    return m, (sd if sd > 1e-9 else 1.0)


# ── coarse lag via normalized cross-correlation of the HR fingerprints ────────

def estimate_lag(ecg_beats: Sequence[float], ppg_beats: Sequence[float],
                 grid_hz: float = 4.0, max_lag_s: float = 120.0,
                 prior_offset_s: Optional[float] = None,
                 prior_window_s: float = 10.0) -> Optional[dict]:
    """
    Coarse offset (seconds) that best aligns the ECG heart-rate curve to the PPG
    heart-rate curve, by normalized cross-correlation. Returns
    {offset_s, corr, n_overlap} or None. `offset_s` is defined so that
    ``t_master ≈ t_ecg_device + offset_s`` (before drift refinement).

    If `prior_offset_s` is given (e.g. from a chest-tap sync marker), the search
    is limited to ±`prior_window_s` around it — disambiguates steady HR.
    """
    et, ev = instantaneous_rate(ecg_beats)
    pt, pv = instantaneous_rate(ppg_beats)
    if len(et) < 3 or len(pt) < 3:
        return None
    dt = 1.0 / grid_hz
    # PPG HR sampled on its own (master-clock) grid.
    pgrid = [pt[0] + i * dt for i in range(int((pt[-1] - pt[0]) / dt) + 1)]
    ppg_hr = _interp(pgrid, pt, pv)
    pm, psd = _zscore(ppg_hr)
    if pm is None:
        return None
    n_ppg_valid = sum(1 for v in ppg_hr if v is not None)
    # Require real overlap so a lag with only a handful of coincidentally-correlated
    # points can't beat the true lag (which overlaps the whole record).
    min_overlap = max(20, int(0.3 * n_ppg_valid))

    if prior_offset_s is not None:
        lo, hi = prior_offset_s - prior_window_s, prior_offset_s + prior_window_s
    else:
        lo, hi = -max_lag_s, max_lag_s
    n_steps = int((hi - lo) / dt) + 1

    best = None
    for k in range(n_steps):
        lag = lo + k * dt
        # ECG HR sampled at (master_time - lag) = device time.
        ecg_hr = _interp(pgrid, et, ev) if lag == 0 else \
            _interp([g - lag for g in pgrid], et, ev)
        em, esd = _zscore(ecg_hr)
        if em is None:
            continue
        s = 0.0
        n = 0
        for a, b in zip(ppg_hr, ecg_hr):
            if a is None or b is None:
                continue
            s += ((a - pm) / psd) * ((b - em) / esd)
            n += 1
        if n < min_overlap:
            continue
        corr = s / n
        if best is None or corr > best['corr']:
            best = {'offset_s': lag, 'corr': round(corr, 4), 'n_overlap': n}
    return best


# ── full alignment: coarse lag + matched-beat least-squares refine ────────────

def align_beats(ecg_beats: Sequence[float], ppg_beats: Sequence[float],
                match_tol_s: float = 0.15, grid_hz: float = 4.0,
                max_lag_s: float = 120.0, prior_offset_s: Optional[float] = None,
                prior_window_s: float = 10.0) -> Optional[dict]:
    """
    Align an ECG beat series (device clock) to a PPG beat series (master clock).
    Returns a dict with the linear map ``t_master = a·t_device + b`` plus quality:
        {a, b, offset_s (=b), drift_ppm, residual_ms, n_matched, match_rate,
         coarse_corr}
    or None if it can't lock. Feed `a`,`b` to remap_times() to move the ECG onto
    the master clock.
    """
    coarse = estimate_lag(ecg_beats, ppg_beats, grid_hz=grid_hz,
                          max_lag_s=max_lag_s, prior_offset_s=prior_offset_s,
                          prior_window_s=prior_window_s)
    if coarse is None:
        return None
    lag = coarse['offset_s']
    ppg_sorted = sorted(ppg_beats)
    ecg_sorted = sorted(ecg_beats)

    # Match each ECG beat (shifted by the coarse lag) to the nearest PPG beat.
    pairs = []          # (t_master_ppg, t_device_ecg)
    j = 0
    for e in ecg_sorted:
        target = e + lag
        while j < len(ppg_sorted) - 1 and \
                abs(ppg_sorted[j + 1] - target) <= abs(ppg_sorted[j] - target):
            j += 1
        if abs(ppg_sorted[j] - target) <= match_tol_s:
            pairs.append((ppg_sorted[j], e))
    if len(pairs) < 3:
        return None
    fit = estimate_offset(pairs)      # fits t_master = a·t_device + b
    if fit is None:
        return None
    a = 1.0 + fit['drift_ppm'] / 1e6
    return {
        'a': a, 'b': fit['offset_s'],
        'offset_s': fit['offset_s'], 'drift_ppm': fit['drift_ppm'],
        'residual_ms': fit['residual_ms'],
        'n_matched': len(pairs),
        'match_rate': round(len(pairs) / max(1, len(ecg_sorted)), 3),
        'coarse_corr': coarse['corr'],
    }


def remap_times(times: Sequence[float], a: float, b: float) -> List[float]:
    """Map device-clock sample times onto the master clock: a·t + b."""
    return [a * t + b for t in times]
