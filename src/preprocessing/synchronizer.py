"""
Temporal Synchronizer
======================
Aligns all recorded modalities (camera, oximeter, CSI, EMG, GSR) onto a single
common timeline and uniform sampling rate, producing analysis-ready output.

Why this exists
---------------
Each modality is logged with its own per-sample timestamp on the PC monotonic
clock (a shared origin set at recording start). CSI is the exception: the raw
``csi_log.csv`` carries the ESP32's *device* clock, which drifts against the PC
clock and resets on power-cycle. The recorder therefore also writes
``csi_timestamped.csv`` pairing each CSI packet with a PC timestamp; this module
uses that anchor to place CSI on the shared axis (and reports the measured
device-vs-PC clock drift as a QA diagnostic).

What it does
------------
1. Loads each modality by *column name* (robust to schema additions like the
   oximeter ``pleth`` column or the camera ``is_real`` flag).
2. Builds a uniform grid at ``target_hz`` over the time span covered by *all*
   present modalities (intersection — no extrapolation).
3. Resamples continuous channels by linear interpolation; maps each grid instant
   to the nearest *real* camera frame (FRC duplicates are skipped via ``is_real``).
4. Writes ``aligned.csv`` + ``sync_report.json`` (and ``aligned.npz`` if numpy is
   available) under ``data/processed/<session_id>/``.

The core is pure standard library so it runs without numpy/pandas. numpy is used
only for the optional ``.npz`` export.

Usage
-----
    python -m src.preprocessing.synchronizer --session data/raw/session_YYYYMMDD_HHMMSS
    python -m src.preprocessing.synchronizer --all --rate 30
"""

import argparse
import csv
import json
import math
import os
from typing import Dict, List, Optional, Tuple

# ─── CSI raw-line layout ─────────────────────────────────────────────────────
# Line: timestamp_ms, seq, rssi, n_carriers, amp0, amp1, ... amp[n_carriers-1]
# The amplitudes are the LAST n_carriers columns, so the start index is derived
# as (len(fields) - n_carriers). This is robust to HT20 (64) vs HT40 (128) and
# fixes the previous hard-coded offset of 6, which dropped the first 2 subcarriers.
CSI_DEVICE_TS_COL = 0     # ESP32 tick (ms)
CSI_RSSI_COL = 2
CSI_NCARRIERS_COL = 3
CSI_MAX_SUBCARRIERS = 52  # keep the first 52 subcarriers


# ─── Pure-stdlib numeric helpers ────────────────────────────────────────────

def _clean(times: List[float], *series: List[float]):
    """Sort by time and drop duplicate timestamps (keep last). Returns
    (times, *series) as parallel lists with strictly increasing times."""
    order = sorted(range(len(times)), key=lambda i: times[i])
    out_t: List[float] = []
    out_s = [[] for _ in series]
    last_t = None
    for i in order:
        t = times[i]
        if last_t is not None and t == last_t:
            # duplicate timestamp — overwrite previous sample
            for k, s in enumerate(series):
                out_s[k][-1] = s[i]
            continue
        out_t.append(t)
        for k, s in enumerate(series):
            out_s[k].append(s[i])
        last_t = t
    return (out_t, *out_s)


def _interp(grid: List[float], xs: List[float], ys: List[float]) -> List[float]:
    """Linear interpolation of (xs, ys) onto grid. xs strictly increasing, grid
    increasing. Values outside [xs[0], xs[-1]] are clamped to the endpoints.
    NaN inputs are ignored (interpolated across)."""
    # Drop NaNs
    if any(v != v for v in ys):
        xs2, ys2 = [], []
        for x, y in zip(xs, ys):
            if y == y:
                xs2.append(x)
                ys2.append(y)
        xs, ys = xs2, ys2
    n = len(xs)
    if n == 0:
        return [float('nan')] * len(grid)
    if n == 1:
        return [ys[0]] * len(grid)
    out = [0.0] * len(grid)
    j = 0
    for i, g in enumerate(grid):
        if g <= xs[0]:
            out[i] = ys[0]
            continue
        if g >= xs[-1]:
            out[i] = ys[-1]
            continue
        while j < n - 2 and xs[j + 1] < g:
            j += 1
        x0, x1 = xs[j], xs[j + 1]
        y0, y1 = ys[j], ys[j + 1]
        out[i] = y0 if x1 == x0 else y0 + (y1 - y0) * (g - x0) / (x1 - x0)
    return out


def _nearest(grid: List[float], xs: List[float], payload: List) -> Tuple[List, List[float]]:
    """For each grid time, return (payload[nearest], xs[nearest]). Two-pointer."""
    n = len(xs)
    out_p, out_t = [], []
    j = 0
    for g in grid:
        while j < n - 1 and abs(xs[j + 1] - g) <= abs(xs[j] - g):
            j += 1
        out_p.append(payload[j])
        out_t.append(xs[j])
    return out_p, out_t


def _fit_clock(dev_ms: List[float], pc_s: List[float]) -> Optional[dict]:
    """Least-squares fit pc_s = a*dev_ms + b. Reports drift in ppm relative to
    the ideal 1 ms→0.001 s slope, and residual std in milliseconds."""
    n = len(dev_ms)
    if n < 2:
        return None
    sx = sum(dev_ms)
    sy = sum(pc_s)
    sxx = sum(x * x for x in dev_ms)
    sxy = sum(x * y for x, y in zip(dev_ms, pc_s))
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    resid = [(y - (a * x + b)) for x, y in zip(dev_ms, pc_s)]
    mean_r = sum(resid) / n
    var = sum((r - mean_r) ** 2 for r in resid) / n
    return {
        'a': a, 'b': b,
        'drift_ppm': round((a / 0.001 - 1.0) * 1e6, 1),
        'residual_ms': round(math.sqrt(var) * 1000.0, 3),
        'n_points': n,
    }


# ─── Loading ────────────────────────────────────────────────────────────────

def _load_named(path: str, ts_name: str = 'timestamp_s',
                filter_col: Optional[str] = None, filter_val: Optional[str] = None):
    """Header-driven CSV loader. Returns (value_names, times, {name: [float]}).
    Non-numeric cells become NaN. Rows can be filtered by an exact column value
    (used to keep only real camera frames). Returns None if unusable."""
    if not os.path.exists(path):
        return None
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or ts_name not in header:
            return None
        idx = {name: i for i, name in enumerate(header)}
        ti = idx[ts_name]
        fi = idx.get(filter_col) if filter_col else None
        value_names = [h for h in header if h != ts_name]
        times: List[float] = []
        data: Dict[str, List[float]] = {v: [] for v in value_names}
        for row in reader:
            if len(row) <= ti:
                continue
            if fi is not None and (len(row) <= fi or row[fi].strip() != filter_val):
                continue
            try:
                t = float(row[ti])
            except ValueError:
                continue
            times.append(t)
            for v in value_names:
                j = idx[v]
                try:
                    data[v].append(float(row[j]) if len(row) > j and row[j] != '' else math.nan)
                except ValueError:
                    data[v].append(math.nan)
        if not times:
            return None
        return value_names, times, data


def _load_csi(session_dir: str):
    """Load CSI from csi_timestamped.csv (PC-clock anchor). Returns
    (pc_times, dev_ms, amps[list-of-rows], rssi[list]) or None if the anchor file
    is missing (old sessions) — CSI cannot be aligned without it."""
    path = os.path.join(session_dir, 'csi', 'csi_timestamped.csv')
    if not os.path.exists(path):
        return None
    pc_times: List[float] = []
    dev_ms: List[float] = []
    amps: List[List[float]] = []
    rssi: List[float] = []
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        next(reader, None)  # header: 'pc_timestamp_s,raw_line'
        for row in reader:
            if len(row) < 2:
                continue
            try:
                pc = float(row[0])
            except ValueError:
                continue
            fields = row[1].split(',')
            if len(fields) <= CSI_NCARRIERS_COL:
                continue
            try:
                d = float(fields[CSI_DEVICE_TS_COL])
                r = float(fields[CSI_RSSI_COL])
                n_carriers = int(float(fields[CSI_NCARRIERS_COL]))
            except (ValueError, IndexError):
                continue
            # Amplitudes are the trailing n_carriers columns.
            amp_start = len(fields) - n_carriers
            if amp_start < CSI_NCARRIERS_COL + 1 or n_carriers <= 0:
                continue
            raw = fields[amp_start:amp_start + CSI_MAX_SUBCARRIERS]
            vec = []
            for x in raw:
                try:
                    vec.append(abs(float(x)))
                except ValueError:
                    vec.append(0.0)
            while len(vec) < CSI_MAX_SUBCARRIERS:
                vec.append(0.0)
            pc_times.append(pc)
            dev_ms.append(d)
            amps.append(vec)
            rssi.append(r)
    if not pc_times:
        return None
    return pc_times, dev_ms, amps, rssi


# ─── Synchronizer ───────────────────────────────────────────────────────────

class SessionSynchronizer:
    """Aligns one session directory to a uniform grid."""

    def __init__(self, session_dir: str, target_hz: float = 30.0,
                 out_root: str = 'data/processed'):
        self.session_dir = os.path.abspath(session_dir)
        self.session_id = os.path.basename(self.session_dir.rstrip('/'))
        self.target_hz = float(target_hz)
        self.out_root = out_root

    def _modality(self, rel, ts='timestamp_s', filter_col=None, filter_val=None):
        return _load_named(os.path.join(self.session_dir, rel),
                           ts_name=ts, filter_col=filter_col, filter_val=filter_val)

    def run(self) -> Optional[dict]:
        warnings: List[str] = []
        modalities: Dict[str, dict] = {}

        oxi = self._modality('oximeter/oximeter_log.csv')
        gsr = self._modality('gsr/gsr_log.csv')
        emg = self._modality('emg/emg_log.csv')
        # Camera: keep only real frames if the is_real column exists
        cam_raw = self._modality('camera/timestamps.csv',
                                 filter_col='is_real', filter_val='1')
        if cam_raw is None:
            cam_raw = self._modality('camera/timestamps.csv')  # older schema
            if cam_raw is not None:
                warnings.append('camera: no is_real column — FRC duplicates not '
                                'excluded (older session schema)')
        csi = _load_csi(self.session_dir)
        if csi is None and os.path.exists(os.path.join(self.session_dir, 'csi', 'csi_log.csv')):
            warnings.append('csi: no csi_timestamped.csv anchor — cannot align to '
                            'PC clock; CSI skipped (session predates dual-timestamp)')

        ranges: List[Tuple[float, float]] = []

        def _note(name, times):
            if not times:
                return
            modalities[name] = {
                'present': True, 'n_input': len(times),
                'first_t': round(min(times), 4), 'last_t': round(max(times), 4),
                'mean_hz': round(len(times) / max(1e-6, max(times) - min(times)), 2),
            }
            ranges.append((min(times), max(times)))

        if oxi:
            _note('oximeter', oxi[1])
        if gsr:
            _note('gsr', gsr[1])
        if emg:
            _note('emg', emg[1])
        if cam_raw:
            _note('camera', cam_raw[1])
        if csi:
            _note('csi', csi[0])

        for nm in ['oximeter', 'gsr', 'emg', 'camera', 'csi']:
            if nm not in modalities:
                modalities[nm] = {'present': False}

        if not ranges:
            return {'session_id': self.session_id, 'error': 'no modalities found',
                    'warnings': warnings}

        t0 = max(r[0] for r in ranges)
        t1 = min(r[1] for r in ranges)
        if t1 <= t0:
            return {'session_id': self.session_id,
                    'error': f'no temporal overlap across modalities (t0={t0:.2f} '
                             f'>= t1={t1:.2f})',
                    'modalities': modalities, 'warnings': warnings}

        n = int((t1 - t0) * self.target_hz)
        grid = [t0 + i / self.target_hz for i in range(n)]

        # ── Assemble aligned columns ──
        columns: "Dict[str, List]" = {'t': grid}

        for tag, loaded in (('oxi', oxi), ('gsr', gsr), ('emg', emg)):
            if not loaded:
                continue
            names, times, data = loaded
            ct, *series = _clean(times, *[data[v] for v in names])
            for v, ys in zip(names, series):
                columns[f'{tag}_{v}'] = _interp(grid, ct, ys)

        camera_report = None
        if cam_raw:
            names, times, data = cam_raw
            fidx = data.get('frame_idx', [float(i) for i in range(len(times))])
            ct, cf = _clean(times, fidx)
            near_idx, near_t = _nearest(grid, ct, cf)
            columns['cam_frame_idx'] = [int(round(x)) for x in near_idx]
            columns['cam_frame_t'] = near_t
            max_err = max((abs(g - nt) for g, nt in zip(grid, near_t)), default=0.0)
            camera_report = {'n_real_frames': len(ct),
                             'max_align_error_ms': round(max_err * 1000, 2)}

        csi_report = None
        if csi:
            pc_times, dev_ms, amps, rssi = csi
            fit = _fit_clock(dev_ms, pc_times)
            cols_amp = [list(c) for c in zip(*amps)] if amps else []
            cleaned = _clean(pc_times, rssi, *cols_amp)
            ct = cleaned[0]
            rssi_c = cleaned[1]
            amp_cols = cleaned[2:]
            columns['csi_rssi'] = _interp(grid, ct, rssi_c)
            for k, ys in enumerate(amp_cols):
                columns[f'csi_amp{k}'] = _interp(grid, ct, list(ys))
            csi_report = {'n_packets': len(pc_times), 'n_subcarriers': len(amp_cols),
                          'clock_fit': fit}

        # ── Write outputs ──
        out_dir = os.path.join(self.out_root, self.session_id)
        os.makedirs(out_dir, exist_ok=True)
        col_names = list(columns.keys())
        csv_path = os.path.join(out_dir, 'aligned.csv')
        with open(csv_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(col_names)
            for i in range(n):
                w.writerow([columns[c][i] for c in col_names])

        npz_written = self._maybe_write_npz(out_dir, columns)

        report = {
            'session_id': self.session_id,
            'target_hz': self.target_hz,
            'n_samples': n,
            'grid_start_s': round(t0, 4),
            'grid_end_s': round(t1, 4),
            'duration_s': round(t1 - t0, 2),
            'n_columns': len(col_names),
            'modalities': modalities,
            'camera': camera_report,
            'csi': csi_report,
            'outputs': {'aligned_csv': csv_path, 'aligned_npz': npz_written},
            'warnings': warnings,
        }
        with open(os.path.join(out_dir, 'sync_report.json'), 'w') as f:
            json.dump(report, f, indent=2)
        return report

    @staticmethod
    def _maybe_write_npz(out_dir, columns) -> Optional[str]:
        """Optional compressed array export for ML — only if numpy is installed."""
        try:
            import numpy as np
        except ImportError:
            return None
        arrays = {c: np.asarray(columns[c]) for c in columns}
        path = os.path.join(out_dir, 'aligned.npz')
        np.savez_compressed(path, **arrays)
        return path


# ─── CLI ────────────────────────────────────────────────────────────────────

def _build_parser():
    p = argparse.ArgumentParser(
        description='Align a recorded session onto a uniform common timeline.')
    p.add_argument('--session', type=str, default=None,
                   help='Path to a session directory (data/raw/session_...)')
    p.add_argument('--all', action='store_true',
                   help='Process every session under --raw-root')
    p.add_argument('--raw-root', type=str, default='data/raw')
    p.add_argument('--out', type=str, default='data/processed')
    p.add_argument('--rate', type=float, default=30.0,
                   help='Target common sampling rate in Hz (default 30)')
    return p


def main():
    args = _build_parser().parse_args()
    targets: List[str] = []
    if args.all:
        if os.path.isdir(args.raw_root):
            targets = [os.path.join(args.raw_root, d)
                       for d in sorted(os.listdir(args.raw_root))
                       if d.startswith('session_')
                       and os.path.isdir(os.path.join(args.raw_root, d))]
    elif args.session:
        targets = [args.session]
    else:
        _build_parser().error('provide --session <dir> or --all')

    if not targets:
        print('No sessions found.')
        return

    for sd in targets:
        sync = SessionSynchronizer(sd, target_hz=args.rate, out_root=args.out)
        try:
            report = sync.run()
        except Exception as e:  # one bad session shouldn't abort a batch
            print(f'  ✗ {os.path.basename(sd)}: {e}')
            continue
        if report is None:
            print(f'  ✗ {os.path.basename(sd)}: nothing to align')
            continue
        if 'error' in report:
            print(f'  ✗ {report["session_id"]}: {report["error"]}')
            continue
        present = [m for m, d in report['modalities'].items() if d.get('present')]
        line = (f'  ✓ {report["session_id"]}: {report["n_samples"]} samples @ '
                f'{report["target_hz"]}Hz, {report["n_columns"]} cols, '
                f'modalities={"+".join(present)}')
        if report.get('csi') and report['csi'].get('clock_fit'):
            line += f", csi_drift={report['csi']['clock_fit']['drift_ppm']}ppm"
        if report.get('warnings'):
            line += f"  ({len(report['warnings'])} warning(s))"
        print(line)


if __name__ == '__main__':
    main()
