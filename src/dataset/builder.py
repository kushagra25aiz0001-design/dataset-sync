"""
Dataset Builder
===============
Converts a processed session into fixed-length, model-ready windows.

Inputs (produced by the earlier stages, under data/processed/<session_id>/):
    aligned.csv     — every modality resampled onto one uniform grid, with a
                      cam_frame_idx column (from synchronizer.py)
    rgb_signal.csv  — per-frame face-ROI mean RGB (from video_preprocessor.py)

What it does
------------
1. Loads aligned.csv (uniform grid) and joins the camera RGB onto the grid via
   cam_frame_idx → rgb_signal.csv.frame_idx.
2. Groups columns into input modalities (camera RGB, CSI subcarriers, EMG, GSR)
   and ground-truth targets from the oximeter — the pleth waveform (per-sample
   regression target) plus per-window heart-rate / SpO2 scalars.
3. Slides fixed-length windows (default 10 s, 50 % overlap) across the grid.
4. Flags each window valid/invalid (face present, finger on probe, no NaNs).
5. Saves windows.npz (if numpy is available) + windows_manifest.json under the
   same processed directory. A later splitter assigns sessions to train/val/test.

The load/join/window/validity core is pure standard library so it is unit-tested
without numpy; numpy is used only to stack and save the .npz tensors.

Usage
-----
    python -m src.dataset.builder --session data/processed/session_YYYYMMDD_HHMMSS
    python -m src.dataset.builder --all --window 10 --stride 5
"""

import argparse
import csv
import json
import math
import os
from typing import Dict, List, Optional, Tuple


# ─── CSV loading (pure stdlib) ───────────────────────────────────────────────

def _load_columns(path: str) -> Optional[Tuple[List[str], Dict[str, list]]]:
    """
    Header-driven load → (header, {col: [...]}). Numeric columns become floats
    (blank/non-numeric → NaN); columns named ``label_*`` (protocol task/cue/
    posture/stimulus labels from the synchronizer) are kept as raw strings.
    """
    if not os.path.exists(path):
        return None
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return None
        is_label = {h: h.startswith('label_') for h in header}
        cols: Dict[str, list] = {h: [] for h in header}
        for row in reader:
            for i, h in enumerate(header):
                val = row[i] if i < len(row) else ''
                if is_label[h]:
                    cols[h].append(val)
                elif val != '':
                    try:
                        cols[h].append(float(val))
                    except ValueError:
                        cols[h].append(math.nan)
                else:
                    cols[h].append(math.nan)
        return header, cols


def _natural_amp_sort(names: List[str]) -> List[str]:
    """Sort csi_amp0, csi_amp1, ... numerically rather than lexically."""
    def key(n):
        digits = ''.join(ch for ch in n if ch.isdigit())
        return int(digits) if digits else 0
    return sorted(names, key=key)


def _mode(values) -> str:
    """Most-common non-empty string in a window (the window's dominant label)."""
    counts: Dict[str, int] = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return ''
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ─── Window math (pure stdlib) ───────────────────────────────────────────────

def _window_slices(n: int, win_len: int, stride: int) -> List[Tuple[int, int]]:
    """Return [(start, end), ...] fully-contained windows of length win_len."""
    if win_len <= 0 or stride <= 0 or n < win_len:
        return []
    out = []
    s = 0
    while s + win_len <= n:
        out.append((s, s + win_len))
        s += stride
    return out


def _median(vals: List[float]) -> float:
    v = sorted(x for x in vals if x == x)  # drop NaN
    if not v:
        return float('nan')
    m = len(v) // 2
    return v[m] if len(v) % 2 else 0.5 * (v[m - 1] + v[m])


def _has_nan(rows: List[List[float]]) -> bool:
    return any(x != x for row in rows for x in row)


# ─── Builder ─────────────────────────────────────────────────────────────────

class WindowBuilder:
    def __init__(self, processed_dir: str, window_s: float = 10.0,
                 stride_s: float = 5.0, min_face_rate: float = 0.5,
                 require_finger: bool = True, min_finger_rate: float = 0.9):
        self.dir = os.path.abspath(processed_dir)
        self.session_id = os.path.basename(self.dir.rstrip('/'))
        self.window_s = float(window_s)
        self.stride_s = float(stride_s)
        self.min_face_rate = float(min_face_rate)
        self.require_finger = require_finger
        self.min_finger_rate = float(min_finger_rate)

    def _grid_rate(self, t: List[float]) -> float:
        # Prefer the synchronizer's recorded target_hz; else infer from the grid.
        rep = os.path.join(self.dir, 'sync_report.json')
        if os.path.exists(rep):
            try:
                with open(rep) as f:
                    hz = json.load(f).get('target_hz')
                if hz:
                    return float(hz)
            except (ValueError, OSError):
                pass
        if len(t) > 1 and t[-1] > t[0]:
            return (len(t) - 1) / (t[-1] - t[0])
        return 30.0

    def run(self) -> dict:
        warnings: List[str] = []
        loaded = _load_columns(os.path.join(self.dir, 'aligned.csv'))
        if loaded is None:
            return {'session_id': self.session_id, 'error': 'no aligned.csv'}
        header, cols = loaded
        n = len(cols['t']) if 't' in cols else 0
        if n == 0:
            return {'session_id': self.session_id, 'error': 'aligned.csv empty'}
        t = cols['t']

        # ── Join camera RGB onto the grid via cam_frame_idx ──
        cam_present = 'cam_frame_idx' in cols
        face = [1.0] * n
        if cam_present:
            rgb = _load_columns(os.path.join(self.dir, 'rgb_signal.csv'))
            if rgb is None:
                warnings.append('no rgb_signal.csv — run video_preprocessor first; '
                                'camera channels omitted')
                cam_present = False
            else:
                _, rcols = rgb
                fmap = {int(fi): k for k, fi in enumerate(rcols['frame_idx'])}
                cam_r, cam_g, cam_b, face = [], [], [], []
                for k in range(n):
                    fi = cols['cam_frame_idx'][k]
                    j = fmap.get(int(fi)) if fi == fi else None
                    if j is None:
                        cam_r.append(math.nan); cam_g.append(math.nan)
                        cam_b.append(math.nan); face.append(0.0)
                    else:
                        cam_r.append(rcols['roi_r'][j])
                        cam_g.append(rcols['roi_g'][j])
                        cam_b.append(rcols['roi_b'][j])
                        face.append(rcols['face_detected'][j]
                                    if 'face_detected' in rcols else 1.0)
                cols['cam_r'], cols['cam_g'], cols['cam_b'] = cam_r, cam_g, cam_b

        # ── Identify input groups + targets present in this session ──
        groups: Dict[str, List[str]] = {}
        if cam_present:
            groups['cam'] = ['cam_r', 'cam_g', 'cam_b']
        csi = _natural_amp_sort([c for c in header if c.startswith('csi_amp')])
        if csi:
            groups['csi'] = csi
        emg = _natural_amp_sort([c for c in header if c.startswith('emg_ch')])
        if emg:
            groups['emg'] = emg
        gsr = [c for c in header if c.startswith('gsr_')]
        if gsr:
            groups['gsr'] = gsr

        pleth_col = 'oxi_pleth' if 'oxi_pleth' in cols else None
        hr_col = 'oxi_heart_rate' if 'oxi_heart_rate' in cols else None
        spo2_col = 'oxi_spo2' if 'oxi_spo2' in cols else None
        if pleth_col is None:
            warnings.append('no oxi_pleth column — older session lacks the pleth '
                            'waveform target; only hr/spo2 scalars available')

        rate = self._grid_rate(t)
        win_len = int(round(self.window_s * rate))
        stride = int(round(self.stride_s * rate))
        slices = _window_slices(n, win_len, stride)

        # Protocol label columns (from synchronizer marker folding), if present
        label_cols = [c for c in cols if c.startswith('label_')]

        # ── Build windows ──
        out_groups: Dict[str, List[List[List[float]]]] = {g: [] for g in groups}
        out_pleth: List[List[float]] = []
        out_hr: List[float] = []
        out_spo2: List[float] = []
        out_tstart: List[float] = []
        out_valid: List[int] = []
        out_labels: Dict[str, List[str]] = {c: [] for c in label_cols}
        n_valid = 0

        for (s, e) in slices:
            valid = True
            # input groups: (win_len, n_channels) row-major
            win_group_rows = {}
            for g, gc in groups.items():
                rows = [[cols[c][i] for c in gc] for i in range(s, e)]
                win_group_rows[g] = rows
                if _has_nan(rows):
                    valid = False
            # camera face presence
            if cam_present:
                fr = sum(face[s:e]) / (e - s)
                if fr < self.min_face_rate:
                    valid = False
            # ground-truth scalars + finger-on fraction. The oximeter writes
            # hr=0 while "searching" (no finger); a window that is mostly
            # finger-off has unreliable ground truth and is invalidated.
            hr_win = [cols[hr_col][i] for i in range(s, e)] if hr_col else []
            spo2_win = [cols[spo2_col][i] for i in range(s, e)] if spo2_col else []
            finger_rate = (sum(1 for v in hr_win if v == v and v > 0) / len(hr_win)
                           if hr_win else 0.0)
            hr_med = _median([v for v in hr_win if v == v and v > 0]) if hr_win else float('nan')
            spo2_med = _median([v for v in spo2_win if v == v and v > 0]) if spo2_win else float('nan')
            if self.require_finger and finger_rate < self.min_finger_rate:
                valid = False
            pleth_win = [cols[pleth_col][i] for i in range(s, e)] if pleth_col else [math.nan] * (e - s)

            for g in groups:
                out_groups[g].append(win_group_rows[g])
            out_pleth.append(pleth_win)
            out_hr.append(hr_med)
            out_spo2.append(spo2_med)
            out_tstart.append(t[s])
            out_valid.append(1 if valid else 0)
            for c in label_cols:
                out_labels[c].append(_mode(cols[c][s:e]))  # window's dominant label
            n_valid += int(valid)

        npz_path = self._save_npz(out_groups, out_pleth, out_hr, out_spo2,
                                  out_tstart, out_valid, out_labels, win_len)

        manifest = {
            'session_id': self.session_id,
            'grid_rate_hz': round(rate, 3),
            'window_s': self.window_s,
            'stride_s': self.stride_s,
            'window_len': win_len,
            'n_windows': len(slices),
            'n_valid': n_valid,
            'valid': out_valid,          # per-window 0/1 flags (loader indexes these)
            'inputs': {g: {'channels': gc, 'n_channels': len(gc)}
                       for g, gc in groups.items()},
            'targets': {
                'pleth': bool(pleth_col),
                'heart_rate': bool(hr_col),
                'spo2': bool(spo2_col),
            },
            'labels': {c: sorted(set(v for v in out_labels[c] if v))
                       for c in label_cols},
            'arrays': {
                **{g: [len(slices), win_len, len(gc)] for g, gc in groups.items()},
                'pleth': [len(slices), win_len],
                'hr': [len(slices)], 'spo2': [len(slices)],
                't_start': [len(slices)], 'valid': [len(slices)],
                **{c: [len(slices)] for c in label_cols},
            },
            'npz': npz_path,
            'warnings': warnings,
        }
        with open(os.path.join(self.dir, 'windows_manifest.json'), 'w') as f:
            json.dump(manifest, f, indent=2)
        return manifest

    def _save_npz(self, out_groups, pleth, hr, spo2, tstart, valid, labels, win_len):
        """Stack and save tensors — only if numpy is available."""
        try:
            import numpy as np
        except ImportError:
            return None
        arrays = {}
        for g, wins in out_groups.items():
            arrays[g] = (np.asarray(wins, dtype='float32')
                         if wins else np.zeros((0, win_len, 0), dtype='float32'))
        arrays['pleth'] = np.asarray(pleth, dtype='float32')
        arrays['hr'] = np.asarray(hr, dtype='float32')
        arrays['spo2'] = np.asarray(spo2, dtype='float32')
        arrays['t_start'] = np.asarray(tstart, dtype='float64')
        arrays['valid'] = np.asarray(valid, dtype='int8')
        for c, vals in labels.items():
            arrays[c] = np.asarray(vals)          # per-window string labels
        path = os.path.join(self.dir, 'windows.npz')
        np.savez_compressed(path, **arrays)
        return path


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser():
    p = argparse.ArgumentParser(
        description='Window a processed session into model-ready training pairs.')
    p.add_argument('--session', type=str, default=None,
                   help='Path to a processed session dir (data/processed/session_...)')
    p.add_argument('--all', action='store_true',
                   help='Process every session under --processed-root')
    p.add_argument('--processed-root', type=str, default='data/processed')
    p.add_argument('--window', type=float, default=10.0, help='Window length (s)')
    p.add_argument('--stride', type=float, default=5.0, help='Window stride (s)')
    p.add_argument('--min-face-rate', type=float, default=0.5)
    p.add_argument('--min-finger-rate', type=float, default=0.9,
                   help='Min fraction of a window with a finger on the probe')
    p.add_argument('--allow-no-finger', action='store_true',
                   help='Keep windows even when no finger is on the oximeter probe')
    return p


def main():
    args = _build_parser().parse_args()
    if args.all:
        root = args.processed_root
        targets = [os.path.join(root, d) for d in sorted(os.listdir(root))
                   if os.path.isdir(os.path.join(root, d))] if os.path.isdir(root) else []
    elif args.session:
        targets = [args.session]
    else:
        _build_parser().error('provide --session <dir> or --all')

    if not targets:
        print('No processed sessions found.')
        return

    for sd in targets:
        try:
            m = WindowBuilder(sd, window_s=args.window, stride_s=args.stride,
                              min_face_rate=args.min_face_rate,
                              require_finger=not args.allow_no_finger,
                              min_finger_rate=args.min_finger_rate).run()
        except Exception as e:
            print(f'  ✗ {os.path.basename(sd)}: {e}')
            continue
        if 'error' in m:
            print(f'  ✗ {m["session_id"]}: {m["error"]}')
            continue
        ins = '+'.join(m['inputs'].keys()) or 'none'
        print(f'  ✓ {m["session_id"]}: {m["n_valid"]}/{m["n_windows"]} valid windows '
              f'({m["window_len"]} samples), inputs={ins}'
              + (f"  ({len(m['warnings'])} warning(s))" if m['warnings'] else ''))


if __name__ == '__main__':
    main()
