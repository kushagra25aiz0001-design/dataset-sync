"""
Sync Audit — is every modality actually on the one master clock?
================================================================
`synchronizer.py` *resamples* modalities onto a common grid. This module answers
the prior question the user asked: **"verify all are in sync or not."** It does
NOT resample; it inspects a raw session directory and, for each of the 11
modalities in :mod:`src.recorder.modalities`, checks that its timing is consistent
with the shared master clock (``time.monotonic() - rec_start``), then reports a
per-modality verdict and one overall cross-modal verdict.

What "in sync" means here
-------------------------
Every captured stream stamps ``timestamp_s = monotonic() - rec_start`` (CSI via a
PC-clock anchor file, audio via a single ``t_start_master_s`` anchor, the
questionnaire/BP via ``t_master_s`` markers). So being in sync reduces to a few
checkable facts:

  1. **Origin** — the stream's first timestamp sits near 0 (same origin), not on a
     foreign clock (e.g. a wall-clock epoch ≈ 1.7e9) and not negative.
  2. **Monotonic** — timestamps don't run backwards.
  3. **Rate** — the effective sampling rate is within the modality's expected band.
  4. **Continuity** — no large dropouts (max inter-sample gap).
  5. **Overlap** — its time span overlaps the window shared by the other streams.

Derived modalities (rPPG, micro-expressions) inherit the facial-video clock, so
their verdict follows the camera's; event modalities (questionnaire, BP) live in
``markers.csv`` and are on the master clock by construction.

Output: ``data/processed/<session_id>/sync_audit.json`` plus a printed table.
Pure standard library (numpy/pandas not required).

Usage
-----
    python -m src.preprocessing.sync_audit --session data/raw/session_YYYYMMDD_HHMMSS
    python -m src.preprocessing.sync_audit --all
"""

import argparse
import csv
import json
import math
import os
import wave
from typing import Dict, List, Optional, Tuple

from src.recorder.modalities import MODALITIES, Modality

# ── Tolerances ───────────────────────────────────────────────────────────────
ORIGIN_OK_S = 3.0        # first sample within this of the origin → clean start
LATE_START_S = 60.0      # first sample beyond ORIGIN_OK_S but under this → late (warn)
FOREIGN_CLOCK_S = 1e6    # first sample beyond this → foreign clock (e.g. epoch)
GAP_ABS_FLOOR_S = 1.0    # any hole > 1 s is worth flagging regardless of rate
GAP_RATE_FACTOR = 50     # ...or > this many nominal periods
BACKWARDS_FRAC = 0.005   # tolerate a few out-of-order samples (< 0.5%)

# Verdict levels, ordered worst → best for aggregation.
_ORDER = {'out_of_sync': 0, 'missing_required': 1, 'degraded': 2,
          'not_computed': 3, 'no_events': 3, 'missing': 4, 'derived': 5,
          'in_sync': 5}


# ── Stdlib numeric helpers ───────────────────────────────────────────────────

def _read_ts(path: str, ts_col: str, real_col: Optional[str] = None
             ) -> Optional[Tuple[List[float], int, int]]:
    """Read the master-clock timestamp column from a CSV.
    Returns (real_times_sorted_input_order, n_total_rows, n_real) or None.
    If `real_col` is present, `real_times` counts only rows where it == '1'
    (genuine samples), while n_total counts every data row."""
    if not os.path.exists(path):
        return None
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or ts_col not in header:
            return None
        ti = header.index(ts_col)
        ri = header.index(real_col) if real_col and real_col in header else None
        times: List[float] = []
        n_total = 0
        for row in reader:
            if len(row) <= ti:
                continue
            try:
                t = float(row[ti])
            except ValueError:
                continue
            n_total += 1
            if ri is not None and (len(row) <= ri or row[ri].strip() != '1'):
                continue
            times.append(t)
        if n_total == 0:
            return None
        return times, n_total, len(times)


def _rate_gaps(times: List[float]) -> dict:
    """Effective rate, max gap, and out-of-order count from timestamps.
    Sorts a copy for gap analysis but measures backwards steps on input order."""
    n = len(times)
    if n < 2:
        return {'n': n, 'rate_hz': 0.0, 'max_gap_s': 0.0, 'span_s': 0.0,
                'first_t': (times[0] if times else None),
                'last_t': (times[0] if times else None), 'n_backwards': 0}
    backwards = sum(1 for i in range(1, n) if times[i] <= times[i - 1])
    s = sorted(times)
    span = s[-1] - s[0]
    max_gap = max((s[i] - s[i - 1] for i in range(1, n)), default=0.0)
    rate = (n - 1) / span if span > 0 else 0.0
    return {'n': n, 'rate_hz': round(rate, 3), 'max_gap_s': round(max_gap, 4),
            'span_s': round(span, 3), 'first_t': round(s[0], 4),
            'last_t': round(s[-1], 4), 'n_backwards': backwards}


def _clock_fit_residual_ms(dev_ms: List[float], pc_s: List[float]) -> Optional[dict]:
    """Least-squares pc_s = a*dev_ms + b; return drift ppm + residual ms.
    Used as a CSI health check (device tick vs PC anchor)."""
    n = len(dev_ms)
    if n < 2:
        return None
    sx, sy = sum(dev_ms), sum(pc_s)
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
    return {'drift_ppm': round((a / 0.001 - 1.0) * 1e6, 1),
            'residual_ms': round(math.sqrt(var) * 1000.0, 3), 'n_points': n}


# ── Per-modality timing verdict ──────────────────────────────────────────────

def _judge_timing(m: Modality, rg: dict, n_total: int) -> Tuple[str, List[str], dict]:
    """Turn rate/gap metrics into (verdict, issues, checks) for a continuous
    per-sample stream. `rg` from _rate_gaps; `n_total` includes FRC duplicates."""
    issues: List[str] = []
    checks: dict = {}
    first_t = rg['first_t']

    # 1) origin
    if first_t is None:
        return 'out_of_sync', ['no usable timestamps'], {'origin': 'none'}
    if first_t < -0.5 or first_t > FOREIGN_CLOCK_S:
        checks['origin'] = 'foreign_clock'
        issues.append(f'first timestamp {first_t:g}s is not on the master clock '
                      f'(expected ~0; looks like a different clock domain)')
        return 'out_of_sync', issues, checks
    if first_t > LATE_START_S:
        checks['origin'] = 'very_late'
        issues.append(f'stream starts {first_t:g}s in — well after the session origin')
    elif first_t > ORIGIN_OK_S:
        checks['origin'] = 'late_start'
        issues.append(f'stream starts {first_t:g}s in (late but same clock)')
    else:
        checks['origin'] = 'ok'

    # 2) monotonic
    checks['monotonic'] = rg['n_backwards'] == 0 or (
        rg['n_backwards'] / max(1, rg['n']) < BACKWARDS_FRAC)
    if not checks['monotonic']:
        issues.append(f"{rg['n_backwards']} out-of-order timestamps "
                      f"({100*rg['n_backwards']/max(1, rg['n']):.1f}%)")

    # 3) rate
    lo, nom, hi = (m.expected_hz or (0.0, 0.0, math.inf))
    rate = rg['rate_hz']
    if lo and rate < lo:
        checks['rate'] = 'low'
        issues.append(f'effective rate {rate:g} Hz below expected {lo:g}-{hi:g} Hz')
    elif hi and rate > hi * 1.5:
        checks['rate'] = 'high'
        issues.append(f'effective rate {rate:g} Hz far above expected {lo:g}-{hi:g} Hz')
    else:
        checks['rate'] = 'ok'

    # 4) continuity
    period = 1.0 / nom if nom else 0.0
    gap_thresh = max(GAP_ABS_FLOOR_S, GAP_RATE_FACTOR * period)
    checks['max_gap_s'] = rg['max_gap_s']
    checks['gap_ok'] = rg['max_gap_s'] <= gap_thresh
    if not checks['gap_ok']:
        issues.append(f"max dropout {rg['max_gap_s']:g}s exceeds {gap_thresh:g}s")

    # FRC duplicate fraction (camera/thermal)
    if m.real_col and n_total > rg['n']:
        checks['frc_duplicate_frac'] = round(1 - rg['n'] / n_total, 3)

    # aggregate
    if checks.get('origin') == 'foreign_clock':
        return 'out_of_sync', issues, checks
    hard = (not checks['monotonic'])
    soft = (checks.get('origin') in ('late_start', 'very_late')
            or checks['rate'] != 'ok' or not checks['gap_ok'])
    if hard:
        return 'out_of_sync', issues, checks
    return ('degraded' if soft else 'in_sync'), issues, checks


# ── Per-category auditors ────────────────────────────────────────────────────

def _audit_per_sample(session_dir: str, m: Modality) -> dict:
    path = os.path.join(session_dir, m.ts_file)
    loaded = _read_ts(path, m.ts_col, m.real_col)
    if loaded is None:
        return _absent(m, path)
    times, n_total, n_real = loaded
    rg = _rate_gaps(times)
    verdict, issues, checks = _judge_timing(m, rg, n_total)
    return {**_base(m), 'present': True, 'verdict': verdict,
            'metrics': {**rg, 'n_total_rows': n_total}, 'checks': checks,
            'issues': issues, 'source_file': m.ts_file}


def _audit_csi(session_dir: str, m: Modality) -> dict:
    anchor = os.path.join(session_dir, m.ts_file)          # csi_timestamped.csv
    raw = os.path.join(session_dir, 'csi', 'csi_log.csv')
    if not os.path.exists(anchor):
        if os.path.exists(raw):
            r = {**_base(m), 'present': True, 'verdict': 'out_of_sync',
                 'metrics': {}, 'checks': {'anchor': 'missing'},
                 'issues': ['csi_log.csv present but no csi_timestamped.csv anchor — '
                            'CSI is on the drifting ESP32 device clock only and '
                            'cannot be placed on the master clock'],
                 'source_file': 'csi/csi_log.csv'}
            return r
        return _absent(m, anchor)

    pc_times: List[float] = []
    dev_ms: List[float] = []
    with open(anchor, newline='', errors='replace') as f:
        reader = csv.reader(f)
        next(reader, None)  # header: pc_timestamp_s,raw_line
        for row in reader:
            if len(row) < 2:
                continue
            try:
                pc_times.append(float(row[0]))
                dev_ms.append(float(row[1].split(',')[0]))
            except (ValueError, IndexError):
                continue
    if not pc_times:
        return _absent(m, anchor)
    rg = _rate_gaps(pc_times)
    verdict, issues, checks = _judge_timing(m, rg, len(pc_times))
    fit = _clock_fit_residual_ms(dev_ms, pc_times)
    if fit:
        checks['clock_fit'] = fit
        if fit['residual_ms'] > 50.0:
            issues.append(f"device-vs-PC clock residual {fit['residual_ms']}ms is high")
            if verdict == 'in_sync':
                verdict = 'degraded'
    return {**_base(m), 'present': True, 'verdict': verdict,
            'metrics': rg, 'checks': checks, 'issues': issues,
            'source_file': m.ts_file}


def _audit_audio(session_dir: str, m: Modality) -> dict:
    meta_path = os.path.join(session_dir, m.ts_file)        # audio/audio_meta.json
    wav_path = os.path.join(session_dir, 'audio', 'audio.wav')
    if not os.path.exists(meta_path) and not os.path.exists(wav_path):
        return _absent(m, meta_path)
    issues: List[str] = []
    checks: dict = {}
    metrics: dict = {}
    meta = None
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except (ValueError, OSError):
            issues.append('audio_meta.json is unreadable')
    t0 = meta.get('t_start_master_s') if meta else None
    sr = meta.get('samplerate') if meta else None
    meta_frames = meta.get('n_frames') if meta else None
    checks['anchor_present'] = t0 is not None
    if t0 is None:
        issues.append('no t_start_master_s anchor — audio cannot be placed on the '
                      'master clock')
        verdict = 'out_of_sync'
    else:
        metrics['first_t'] = round(float(t0), 4)
        if t0 < -0.5 or t0 > FOREIGN_CLOCK_S:
            checks['origin'] = 'foreign_clock'
            issues.append(f'audio anchor {t0:g}s is not on the master clock')
            verdict = 'out_of_sync'
        else:
            checks['origin'] = 'ok' if t0 <= ORIGIN_OK_S else 'late_start'
            verdict = 'in_sync' if t0 <= LATE_START_S else 'degraded'
            if t0 > ORIGIN_OK_S:
                issues.append(f'audio starts {t0:g}s in')

    # Cross-check the WAV against the metadata (frame count / duration).
    if os.path.exists(wav_path):
        try:
            with wave.open(wav_path, 'rb') as wf:
                wav_frames = wf.getnframes()
                wav_sr = wf.getframerate()
            metrics['wav_frames'] = wav_frames
            metrics['samplerate'] = wav_sr
            metrics['duration_s'] = round(wav_frames / wav_sr, 3) if wav_sr else 0
            if t0 is not None:
                metrics['last_t'] = round(float(t0) + metrics['duration_s'], 4)
            if meta_frames not in (None, wav_frames):
                issues.append(f'meta n_frames {meta_frames} != wav frames {wav_frames}')
                if verdict == 'in_sync':
                    verdict = 'degraded'
            if sr and wav_sr and sr != wav_sr:
                issues.append(f'meta samplerate {sr} != wav {wav_sr}')
        except (wave.Error, OSError) as e:
            issues.append(f'unreadable audio.wav: {e}')
            if verdict == 'in_sync':
                verdict = 'degraded'
    else:
        checks['wav_present'] = False
        issues.append('audio_meta.json present but audio.wav missing')
        verdict = 'out_of_sync'
    return {**_base(m), 'present': True, 'verdict': verdict,
            'metrics': metrics, 'checks': checks, 'issues': issues,
            'source_file': m.ts_file}


def _audit_event(session_dir: str, m: Modality) -> dict:
    """Questionnaire / BP live in markers.csv on the master clock."""
    path = os.path.join(session_dir, 'markers.csv')
    if not os.path.exists(path):
        return {**_base(m), 'present': False, 'verdict': 'no_events',
                'metrics': {}, 'checks': {}, 'issues': ['no markers.csv'],
                'source_file': 'markers.csv'}
    # Count markers relevant to this modality.
    if m.id == 'bp':
        relevant_prefixes = ('rating:BP', 'bp')
    else:  # questionnaire
        relevant_prefixes = ('rating:', 'response:')
    times: List[float] = []
    n_relevant = 0
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or 't_master_s' not in header or 'label' not in header:
            return {**_base(m), 'present': False, 'verdict': 'no_events',
                    'metrics': {}, 'checks': {}, 'issues': ['markers.csv malformed'],
                    'source_file': 'markers.csv'}
        ti, li = header.index('t_master_s'), header.index('label')
        for row in reader:
            if len(row) <= max(ti, li):
                continue
            try:
                t = float(row[ti])
            except ValueError:
                continue
            times.append(t)
            if any(row[li].startswith(p) for p in relevant_prefixes):
                n_relevant += 1
    issues = []
    checks = {'n_markers_total': len(times), 'n_relevant': n_relevant}
    if times:
        checks['origin'] = 'ok' if -0.5 <= min(times) <= FOREIGN_CLOCK_S else 'foreign_clock'
    verdict = 'in_sync' if n_relevant > 0 else 'no_events'
    if n_relevant == 0:
        issues.append(f'no {m.id} events found in markers.csv')
    return {**_base(m), 'present': n_relevant > 0, 'verdict': verdict,
            'metrics': {'first_t': (round(min(times), 4) if times else None),
                        'last_t': (round(max(times), 4) if times else None)},
            'checks': checks, 'issues': issues, 'source_file': 'markers.csv'}


def _audit_derived(session_dir: str, m: Modality, results: Dict[str, dict]) -> dict:
    """rPPG / micro-expressions inherit the source (facial video) clock. If the
    derived product exists, note it; otherwise it is simply 'not_computed'."""
    src = results.get(m.derived_from)
    src_verdict = src['verdict'] if src else 'missing'
    issues = [f'inherits timing from {m.derived_from} (verdict: {src_verdict})']
    # Look for a computed product (rPPG rgb_signal) under data/processed.
    computed = False
    if m.id == 'rppg':
        proc = os.path.join('data', 'processed',
                            os.path.basename(session_dir.rstrip('/')), 'rgb_signal.csv')
        computed = os.path.exists(proc)
        if computed:
            issues.append('rgb_signal.csv present')
    verdict = 'derived' if src_verdict in ('in_sync', 'degraded') else 'not_computed'
    if src_verdict == 'out_of_sync':
        verdict = 'out_of_sync'
        issues.append(f'source {m.derived_from} is out of sync — derived signal '
                      f'would inherit the error')
    return {**_base(m), 'present': bool(src and src.get('present')),
            'verdict': verdict, 'metrics': {}, 'checks': {'computed': computed},
            'issues': issues, 'source_file': m.ts_file or ''}


# ── small builders ───────────────────────────────────────────────────────────

def _base(m: Modality) -> dict:
    return {'id': m.id, 'label': m.label, 'category': m.category, 'tier': m.tier}


def _absent(m: Modality, path: str) -> dict:
    verdict = 'missing_required' if m.required else 'missing'
    return {**_base(m), 'present': False, 'verdict': verdict, 'metrics': {},
            'checks': {}, 'issues': [f'no data at {os.path.relpath(path, ".")}'],
            'source_file': m.ts_file or ''}


# ── The audit ────────────────────────────────────────────────────────────────

class SessionSyncAudit:
    """Audit one session directory for master-clock co-registration."""

    def __init__(self, session_dir: str, out_root: str = 'data/processed'):
        self.session_dir = os.path.abspath(session_dir)
        self.session_id = os.path.basename(self.session_dir.rstrip('/'))
        self.out_root = out_root

    def run(self) -> dict:
        results: Dict[str, dict] = {}
        # Order matters: raw first (so derived can read source verdicts).
        for m in [x for x in MODALITIES if x.category == 'raw']:
            results[m.id] = self._audit_one(m, results)
        for m in [x for x in MODALITIES if x.category == 'event']:
            results[m.id] = self._audit_one(m, results)
        for m in [x for x in MODALITIES if x.category == 'derived']:
            results[m.id] = self._audit_one(m, results)

        cross = self._cross_modal(results)
        report = {
            'session_id': self.session_id,
            'session_dir': self.session_dir,
            'overall': cross['overall'],
            'cross_modal': cross,
            'modalities': results,
        }
        out_dir = os.path.join(self.out_root, self.session_id)
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, 'sync_audit.json'), 'w') as f:
            json.dump(report, f, indent=2)
        report['_out'] = os.path.join(out_dir, 'sync_audit.json')
        return report

    def _audit_one(self, m: Modality, results: Dict[str, dict]) -> dict:
        if m.category == 'derived':
            return _audit_derived(self.session_dir, m, results)
        if m.category == 'event':
            return _audit_event(self.session_dir, m)
        if m.clock == 'device+pc_anchor':
            return _audit_csi(self.session_dir, m)
        if m.clock == 'anchor_meta':
            return _audit_audio(self.session_dir, m)
        return _audit_per_sample(self.session_dir, m)

    def _cross_modal(self, results: Dict[str, dict]) -> dict:
        """Are the present continuous streams co-registered on one clock?"""
        present_raw = [r for r in results.values()
                       if r['category'] == 'raw' and r.get('present')
                       and r['metrics'].get('first_t') is not None]
        firsts = [r['metrics']['first_t'] for r in present_raw]
        lasts = [r['metrics'].get('last_t') for r in present_raw
                 if r['metrics'].get('last_t') is not None]
        info: dict = {
            'n_present_raw': len(present_raw),
            'streams': [r['id'] for r in present_raw],
        }
        if firsts and lasts:
            overlap_start = max(firsts)
            overlap_end = min(lasts)
            info['start_skew_ms'] = round((max(firsts) - min(firsts)) * 1000, 1)
            info['overlap_s'] = round(overlap_end - overlap_start, 3)
            info['overlap_window'] = [round(overlap_start, 3), round(overlap_end, 3)]

        # Overall verdict.
        verdicts = [r['verdict'] for r in results.values()]
        any_out = any(v == 'out_of_sync' for v in verdicts)
        any_degraded = any(v == 'degraded' for v in verdicts)
        missing_required = [r['id'] for r in results.values()
                            if r['verdict'] == 'missing_required']
        no_overlap = firsts and lasts and info.get('overlap_s', 1) <= 0

        if any_out or no_overlap or len(present_raw) < 2:
            overall = 'FAIL'
        elif any_degraded or missing_required:
            overall = 'WARN'
        else:
            overall = 'PASS'
        reasons = []
        if len(present_raw) < 2:
            reasons.append(f'only {len(present_raw)} raw stream(s) present — '
                           f'nothing to cross-register')
        if any_out:
            reasons.append('one or more streams are out of sync')
        if no_overlap:
            reasons.append('streams have no common time window')
        if missing_required:
            reasons.append('missing required: ' + ', '.join(missing_required))
        if any_degraded and overall != 'FAIL':
            reasons.append('one or more streams degraded (see issues)')
        info['overall'] = overall
        info['reasons'] = reasons
        return info

    # ── rendering ────────────────────────────────────────────────────────────

    def render(self, report: dict) -> str:
        icon = {'in_sync': '✅', 'degraded': '⚠️ ', 'out_of_sync': '❌',
                'missing': '⬛', 'missing_required': '❌', 'not_computed': '·',
                'no_events': '·', 'derived': '↳'}
        lines = [
            f'\nSync audit — {report["session_id"]}',
            '=' * 66,
        ]
        header = f'     {"modality":<26} {"cat":<8} {"tier":<4} {"verdict":<12} rate/notes'
        lines.append(header)
        lines.append('  ' + '-' * 62)
        for m in MODALITIES:
            r = report['modalities'][m.id]
            v = r['verdict']
            met = r.get('metrics', {})
            extra = ''
            if met.get('rate_hz'):
                extra = f"{met['rate_hz']:g} Hz"
            elif met.get('samplerate'):
                extra = f"{met['samplerate']}Hz audio"
            elif r['checks'].get('n_relevant') is not None:
                extra = f"{r['checks']['n_relevant']} events"
            if r.get('issues') and v in ('out_of_sync', 'degraded'):
                extra = (extra + ' — ' if extra else '') + r['issues'][0]
            # Label truncated to a fixed field so columns line up regardless of the
            # (variable display-width) status glyph in the left margin.
            lines.append(f'  {icon.get(v, "? "):<3}{m.label:<26.26} {m.category:<8} '
                         f'{m.tier:<4} {v:<12} {extra}')
        cm = report['cross_modal']
        lines.append('  ' + '-' * 62)
        if 'overlap_s' in cm:
            lines.append(f'  cross-modal: {cm["n_present_raw"]} raw streams, '
                         f'start skew {cm.get("start_skew_ms", "?")} ms, '
                         f'common window {cm.get("overlap_s", "?")} s')
        badge = {'PASS': '✅ PASS', 'WARN': '⚠️  WARN', 'FAIL': '❌ FAIL'}[report['overall']]
        lines.append(f'\n  OVERALL: {badge}')
        for reason in cm.get('reasons', []):
            lines.append(f'    • {reason}')
        lines.append('')
        return '\n'.join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_parser():
    p = argparse.ArgumentParser(
        description='Audit a session: are all modalities on the one master clock?')
    p.add_argument('--session', type=str, default=None,
                   help='Path to a session directory (data/raw/session_...)')
    p.add_argument('--all', action='store_true',
                   help='Audit every session under --raw-root')
    p.add_argument('--raw-root', type=str, default='data/raw')
    p.add_argument('--out', type=str, default='data/processed')
    p.add_argument('--json', action='store_true',
                   help='Print the JSON report instead of the table')
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

    worst = 'PASS'
    for sd in targets:
        audit = SessionSyncAudit(sd, out_root=args.out)
        try:
            report = audit.run()
        except Exception as e:  # one bad session shouldn't abort a batch
            print(f'  ✗ {os.path.basename(sd)}: {e}')
            worst = 'FAIL'
            continue
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(audit.render(report))
        rank = {'PASS': 0, 'WARN': 1, 'FAIL': 2}
        if rank[report['overall']] > rank[worst]:
            worst = report['overall']
    raise SystemExit(0 if worst != 'FAIL' else 1)


if __name__ == '__main__':
    main()
