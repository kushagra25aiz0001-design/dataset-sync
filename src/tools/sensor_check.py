"""
Per-Modality Sensor Smoke Checker
=================================
Test ONE or TWO modalities in isolation **using the real handler code**, before
adding them to the full cluster. The problem this solves: in the live dashboard
all sensor handlers share one Flask-SocketIO event loop and one registry; a single
misbehaving modality (a port that won't open, a stream that stalls or floods, a
sensor that silently emits *mock* data) lags the whole UI and skews every stream's
timestamps — so the collected dataset is subtly out of sync and unusable.

This tool runs the selected handler(s) with a throwaway registry and a capturing
fake-`sio` (NO Flask, NO web event loop — so it can't reintroduce that shared-loop
lag), watches each stream for a few seconds, and prints a hard PASS/WARN/FAIL
report card per modality:

    open?  streaming?  achieved Hz vs the manifest's expected band  rate stability
    longest gap / stall  data quality (real, non-degenerate values)

and, for a pair, a co-run sync check (start skew + simultaneous liveness) so you
know whether the two are safe to run together.

Truth sources (why it can't be fooled):
  • Liveness = the **registry sample counter** (serial) or the handler's
    **measured_fps** (camera/thermal — their counter only advances while writing
    to disk). EMG's mock feed emits socket data but never advances the counter, so
    it is correctly reported as NOT streaming real data.
  • Data quality = the actual values the handler emits, gated on real streaming.

Examples:
    python -m src.tools.sensor_check --list
    python -m src.tools.sensor_check --only gsr
    python -m src.tools.sensor_check --only gsr,oximeter -t 15
    python -m src.tools.sensor_check --only emg --emg-port /dev/ttyUSB0
    python -m src.tools.sensor_check --only camera --camera-source /dev/video0
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
import threading
from collections import defaultdict

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.recorder.sensor_registry import SensorRegistry, SensorState, ALL_SENSOR_NAMES
from src.recorder.modalities import BY_ID

# Live sensors that have a handler with a run() loop (or, for audio, a recorder).
TESTABLE = ('camera', 'oximeter', 'csi', 'emg', 'gsr', 'thermal', 'audio')

# sensor name → manifest id (for the expected-Hz band + sync tier).
SENSOR_TO_MODALITY = {
    'camera': 'facial_video', 'oximeter': 'oximeter', 'csi': 'wifi_csi',
    'emg': 'emg', 'gsr': 'gsr', 'thermal': 'thermal', 'audio': 'audio',
}
# The socket event each numeric sensor emits its samples on (for data quality).
DATA_EVENT = {'oximeter': 'oxi_data', 'gsr': 'gsr_data',
              'emg': 'emg_data', 'csi': 'csi_data'}
# camera/thermal expose a live rate, not a monitoring counter.
RATE_SOURCED = ('camera', 'thermal')
# Keys that are indices/counters, not measured signal — excluded from data quality.
INDEX_KEYS = {'n', 'seq', 'idx', 'index', 'count', 'ts', 't', 'timestamp',
              'timestamp_s', 'frame', 'frame_idx', 'id', 'cal_progress'}

OK, WARN, FAIL = 'PASS', 'WARN', 'FAIL'
_RANK = {OK: 0, WARN: 1, FAIL: 2}


# ── capturing fake socketio ───────────────────────────────────────────────────

class CaptureSio:
    """Stands in for Flask-SocketIO. Handlers only call `.emit`; we record the
    device_status transitions and the data payloads (with arrival time) so we can
    judge liveness/quality offline. Thread-safe; extra no-op methods for safety."""

    def __init__(self):
        self._lock = threading.Lock()
        self.data = defaultdict(list)     # event -> [(t_monotonic, payload)]
        self.status = defaultdict(list)   # device -> [(t, ok, msg)]

    def emit(self, event, data=None, *args, **kwargs):
        t = time.monotonic()
        with self._lock:
            if event == 'device_status' and isinstance(data, dict):
                self.status[data.get('device', '?')].append(
                    (t, data.get('ok'), data.get('msg', '')))
            else:
                self.data[event].append((t, data))

    # SocketIO API compatibility (unused by the handlers we drive, but safe):
    def on(self, *a, **k):
        return lambda f: f

    def start_background_task(self, target, *a, **k):
        th = threading.Thread(target=target, args=a, kwargs=k, daemon=True)
        th.start()
        return th

    def sleep(self, s):
        time.sleep(s)


# ── data-quality: flatten measured numeric leaves from an emitted payload ─────

def _numeric_leaves(obj):
    if isinstance(obj, bool):
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)]
    if isinstance(obj, dict):
        out = []
        for k, v in obj.items():
            if str(k).lower() in INDEX_KEYS:
                continue
            out += _numeric_leaves(v)
        return out
    if isinstance(obj, (list, tuple)):
        out = []
        for v in obj:
            out += _numeric_leaves(v)
        return out
    return []


def _data_quality(sio: CaptureSio, name: str, since_mono: float):
    """Return (label, n_samples, note). label ∈ ok/flat/zero/nan/none/na.

    Flat-line is judged PER FIELD over time: a real signal has at least one value
    that changes across samples. Pooling all fields would hide a flat-lined sensor
    whose fields are constant at *different* values (e.g. uS=3.14, raw=1800)."""
    event = DATA_EVENT.get(name)
    if event is None:
        return ('na', 0, 'visual stream (frame-rate is the metric)')
    payloads = [p for (t, p) in sio.data.get(event, []) if t >= since_mono]
    if not payloads:
        return ('none', 0, 'no sample events surfaced while streaming')
    rows = [r for r in (_numeric_leaves(p) for p in payloads) if r]
    all_vals = [v for r in rows for v in r]
    if not all_vals:
        return ('none', len(payloads), 'events carried no numeric values')
    if not all(math.isfinite(v) for v in all_vals):
        return ('nan', len(payloads), 'contains NaN/inf — corrupt parse')
    if all(v == 0 for v in all_vals):
        return ('zero', len(payloads), 'every value is 0 — dead sensor/wrong port')
    # per-position temporal variance when the payload width is stable
    widths = {len(r) for r in rows}
    if len(rows) > 1 and len(widths) == 1:
        w = widths.pop()
        varies = any(len({round(r[j], 6) for r in rows}) > 1 for j in range(w))
    else:  # variable width (e.g. CSI carrier count) → pooled fallback
        varies = len({round(v, 6) for v in all_vals}) > 1
    if not varies:
        return ('flat', len(payloads), 'constant value — flat-line, not a live signal')
    return ('ok', len(payloads), '')


# ── handler construction (lazy imports so one missing dep can't block others) ──

def build_handler(name, args, registry, sio):
    if name == 'camera':
        from src.dashboard.handlers.camera_handler import CameraHandler
        return CameraHandler(registry, sio, source=args.camera_source,
                             resolution=args.cam_res, record_format='video')
    if name == 'oximeter':
        from src.dashboard.handlers.oximeter_handler import OximeterHandler
        return OximeterHandler(registry, sio, port_cfg=args.oxi_port)
    if name == 'csi':
        from src.dashboard.handlers.csi_handler import CSIHandler
        return CSIHandler(registry, sio, port=args.csi_port, baud=args.csi_baud)
    if name == 'emg':
        from src.dashboard.handlers.emg_handler import EMGHandler
        return EMGHandler(registry, sio, port=args.emg_port, baud=args.emg_baud)
    if name == 'gsr':
        from src.dashboard.handlers.gsr_handler import GSRHandler
        return GSRHandler(registry, sio, port=args.gsr_port, baud=args.gsr_baud)
    if name == 'thermal':
        from src.dashboard.handlers.thermal_handler import ThermalHandler
        return ThermalHandler(registry, sio, source=args.thermal_source)
    raise ValueError(name)


def maybe_assign_ports(live, args, registry, sio):
    """gsr/emg/csi in 'auto' mode need the orchestrator to probe + assign a serial
    port (oximeter self-detects, camera/thermal are source-based). Scoped to the
    selected sensors; everything else is 'none' so we don't disturb other devices.
    Returns the orchestrator (to stop later) or None."""
    port_of = {'oximeter': args.oxi_port, 'csi': args.csi_port,
               'emg': args.emg_port, 'gsr': args.gsr_port}
    need_auto = [n for n in live if n in ('gsr', 'emg', 'csi')
                 and port_of.get(n) == 'auto']
    if not need_auto:
        return None
    from src.recorder.sensor_orchestrator import SensorOrchestrator
    orch = SensorOrchestrator(registry, sio)
    assignments = {n: 'none' for n in ALL_SENSOR_NAMES}
    for n in live:
        if n in port_of:
            assignments[n] = port_of[n]
    bauds = {'csi': args.csi_baud, 'emg': args.emg_baud, 'gsr': args.gsr_baud}
    print(f"  · auto-detecting serial port(s) for {', '.join(need_auto)} "
          f"(probing available ports)…")
    try:
        orch.discover_all(assignments, bauds)
    except Exception as e:  # detection must never crash the check
        print(f"  · port auto-detect error: {e}")
    return orch


# ── live monitor loop (drives 1..N live handlers concurrently) ────────────────

def _reading(name, handler, registry):
    """(kind, value, state, msg): kind 'rate' → value is fps now; 'count' → value
    is the cumulative registry sample counter."""
    info = registry.get_sensor(name)
    state = info.state.value if info else 'unknown'
    msg = (info.status_msg or '') if info else ''
    if name in RATE_SOURCED:
        return ('rate', float(getattr(handler, 'measured_fps', 0.0) or 0.0), state, msg)
    return ('count', float(registry.get_counter(name)), state, msg)


def monitor(live, handlers, registry, args):
    """Run the handlers for args.duration, polling every args.poll seconds.
    Returns (polls, t0_monotonic). `polls` = [{t, <name>: (kind,value,state,msg)}]."""
    threads = {}
    for n, h in handlers.items():
        th = threading.Thread(target=_guarded_run, args=(h, n, ERRORS),
                              daemon=True, name='chk-' + n)
        th.start()
        threads[n] = th

    polls = []
    t0 = time.monotonic()
    live_tty = sys.stdout.isatty() and not args.no_live
    board_lines = 0
    try:
        while time.monotonic() - t0 < args.duration:
            now = time.monotonic()
            row = {'t': now - t0}
            for n, h in handlers.items():
                row[n] = _reading(n, h, registry)
            polls.append(row)
            board_lines = _render_live(row, live, t0, args, live_tty, board_lines)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        print("\n  · interrupted — stopping and reporting what we have…")
    finally:
        for h in handlers.values():
            try:
                h.stop()
            except Exception:
                pass
        for n, th in threads.items():
            th.join(timeout=5.0 if n == 'oximeter' else 2.0)
    if live_tty:
        print()
    return polls, t0


ERRORS = {}  # name -> exception string, filled by _guarded_run


def _guarded_run(handler, name, errors):
    try:
        handler.run()
    except Exception as e:  # a crash in run() → recorded, surfaced in the report
        errors[name] = f'{type(e).__name__}: {e}'


def _render_live(row, live, t0, args, live_tty, prev_lines):
    elapsed = row['t']
    cells = []
    for n in live:
        kind, val, state, _ = row[n]
        rate = val if kind == 'rate' else None
        tag = f"{rate:5.1f}fps" if rate is not None else f"n={int(val):>6}"
        cells.append(f"{n}:{state[:4]}:{tag}")
    line = f"  ⏱ {elapsed:4.1f}/{args.duration:.0f}s  " + "   ".join(cells)
    if live_tty:
        if prev_lines:
            sys.stdout.write('\r')
        sys.stdout.write('\033[K' + line)
        sys.stdout.flush()
        return 1
    # non-tty: print a sparse progress line so logs stay readable
    if int(elapsed * 2) % 4 == 0:
        print(line)
    return 0


# ── metric computation from the poll series ───────────────────────────────────

def compute_metrics(name, polls, sio, t0, args):
    series = [(p['t'], p[name]) for p in polls if name in p]
    band = BY_ID[SENSOR_TO_MODALITY[name]].expected_hz  # (min, nom, max)
    tier = BY_ID[SENSOR_TO_MODALITY[name]].tier

    # instantaneous Hz per poll: rate sources already give fps; counters → deltas.
    # first_t is detected from the RAW readings (first frame/first count advance),
    # NOT the delta series, so a counter isn't reported one poll later than a rate
    # source — that would fake a start-skew between them.
    inst = []      # (t, hz)
    first_t = None
    ever_state = set()
    last_msg = ''
    prev = None
    base_count = None
    for (t, (kind, val, state, msg)) in series:
        ever_state.add(state)
        last_msg = msg or last_msg
        if kind == 'rate':
            hz = val
            if val > 0 and first_t is None:
                first_t = t
        else:
            if base_count is None:
                base_count = val
                if val > 0:                       # already counting at the first poll
                    first_t = t
            elif val > base_count and first_t is None:
                first_t = t                       # first real advance
            hz = None if prev is None else (
                (val - prev[1]) / (t - prev[0]) if t > prev[0] else 0.0)
            prev = (t, val)
        if hz is not None:
            inst.append((t, max(0.0, hz)))

    win = [(t, hz) for (t, hz) in inst if t >= args.settle]
    hzs = [hz for (_, hz) in win]
    streaming = any(hz > 0 for (_, hz) in inst)
    opened = streaming or bool(ever_state & {'streaming', 'connected', 'detected'})
    achieved = statistics.mean(hzs) if hzs else 0.0
    cov = (statistics.pstdev(hzs) / achieved) if (len(hzs) > 1 and achieved > 0) else 0.0

    # longest contiguous stretch of zero throughput within the window
    max_gap, run, prev_t = 0.0, 0.0, None
    for (t, hz) in win:
        if prev_t is not None:
            dt = t - prev_t
            run = run + dt if hz == 0 else 0.0
            max_gap = max(max_gap, run)
        prev_t = t

    dq_label, dq_n, dq_note = (('na', 0, '') if not streaming
                               else _data_quality(sio, name, t0 + args.settle))
    if not streaming:
        dq_label = 'na'

    # Did the handler fall back to a SYNTHETIC feed? (EMG emits mock packets over
    # the socket when its port won't open — never advancing the real counter. We
    # flag it so a fabricated stream is never mistaken for a live sensor.)
    mock_seen = any('mock' in (msg or '').lower()
                    for (_t, _ok, msg) in sio.status.get(name, []))

    m = dict(name=name, tier=tier, band=band, opened=opened, streaming=streaming,
             achieved_hz=achieved, cov=cov, max_gap=max_gap, first_t=first_t,
             state=last_state(series), msg=last_msg, dq=dq_label, dq_n=dq_n,
             dq_note=dq_note, error=ERRORS.get(name), mock_seen=mock_seen)
    m['issues'], m['verdict'] = _verdict(name, m, args)
    return m


def last_state(series):
    return series[-1][1][2] if series else 'unknown'


def _verdict(name, m, args):
    issues = []
    band = m['band']
    if m['error']:
        issues.append((FAIL, f"handler crashed: {m['error']}"))
    if not m['opened']:
        issues.append((FAIL, f"did not open (state={m['state']}"
                             + (f", '{m['msg']}'" if m['msg'] else '') + ')'))
        if m.get('mock_seen'):
            issues.append((WARN, "the dashboard shows a SYNTHETIC mock feed for this "
                                 "device — a real session would record fabricated data"))
    elif not m['streaming']:
        mock = 'mock' in (m['msg'] or '').lower()
        issues.append((FAIL, "opened but produced NO real samples"
                       + (" — UI shows a MOCK feed, not real data" if mock else
                          f" (state={m['state']})")))
    else:
        lo, _nom, hi = band if band else (None, None, None)
        if lo is not None and m['achieved_hz'] < lo:
            issues.append((WARN, f"slow: {m['achieved_hz']:.1f} Hz < {lo:g} Hz min "
                                 f"(this is the kind of lag that desyncs the cluster)"))
        elif hi is not None and m['achieved_hz'] > hi * 1.15:
            issues.append((WARN, f"fast: {m['achieved_hz']:.1f} Hz > {hi:g} Hz max "
                                 f"(flooding — can starve other streams)"))
        if m['max_gap'] >= args.stall:
            issues.append((FAIL, f"stalled: {m['max_gap']:.1f}s with no samples"))
        elif m['max_gap'] >= 1.0:
            issues.append((WARN, f"gap: {m['max_gap']:.1f}s pause in the stream"))
        if m['cov'] >= 0.5:
            issues.append((WARN, f"jittery: rate varies ±{m['cov']*100:.0f}% "
                                 f"(bursty delivery, not steady)"))
        if m['dq'] == 'nan':
            issues.append((FAIL, f"bad data: {m['dq_note']}"))
        elif m['dq'] in ('zero', 'flat'):
            issues.append((WARN, f"suspect data: {m['dq_note']}"))
        elif m['dq'] == 'none':
            issues.append((WARN, f"data: {m['dq_note']}"))
    verdict = OK
    for lvl, _ in issues:
        if _RANK[lvl] > _RANK[verdict]:
            verdict = lvl
    return issues, verdict


# ── audio (recorder-owned; no run() loop) ─────────────────────────────────────

def check_audio(args):
    import tempfile
    import wave
    band = BY_ID['audio'].expected_hz
    out = tempfile.mkdtemp(prefix='sensorchk_audio_')
    m = dict(name='audio', tier=BY_ID['audio'].tier, band=band, opened=False,
             streaming=False, achieved_hz=0.0, cov=0.0, max_gap=0.0, first_t=0.0,
             state='-', msg='', dq='na', dq_n=0, dq_note='', error=None, issues=[])
    try:
        from src.recorder.audio_recorder import AudioRecorder
        rec = AudioRecorder(out, time.monotonic(), device=args.audio_device)
        print(f"  · capturing audio for {args.duration:.0f}s "
              f"(device={args.audio_device or 'system default'})…")
        rec.start()
        m['opened'] = True
        t0 = time.monotonic()
        while time.monotonic() - t0 < args.duration:
            time.sleep(min(0.5, args.duration))
        meta = rec.stop()
        m['state'] = meta.get('device_name') or 'default'
        m['msg'] = f"backend={meta.get('backend', 'sounddevice')}"
        sr = meta.get('samplerate') or 0
        n = meta.get('n_frames') or 0
        m['streaming'] = n > 0
        m['achieved_hz'] = sr
        # data quality: peek the WAV for a real (non-silent) signal
        wav = os.path.join(out, 'audio.wav')
        if n > 0 and os.path.exists(wav):
            with wave.open(wav, 'rb') as wf:
                frames = wf.readframes(min(wf.getnframes(), sr))  # up to 1s
            import struct
            if frames:
                cnt = len(frames) // 2
                vals = struct.unpack('<%dh' % cnt, frames[:cnt * 2])
                peak = max(abs(v) for v in vals) if vals else 0
                if peak == 0:
                    m['dq'], m['dq_note'] = 'zero', 'digital silence (peak=0)'
                elif peak < 30:
                    m['dq'], m['dq_note'] = 'flat', f'near-silent (peak={peak})'
                else:
                    m['dq'], m['dq_note'] = 'ok', f'peak={peak}'
    except Exception as e:
        m['error'] = f'{type(e).__name__}: {e}'
    # verdict
    issues = []
    if m['error']:
        issues.append((FAIL, f"audio backend error: {m['error']}"))
    elif not m['streaming']:
        issues.append((FAIL, "captured 0 frames (no mic / wrong device)"))
    else:
        lo = m['band'][0] if m['band'] else None
        if lo and m['achieved_hz'] < lo:
            issues.append((WARN, f"low sample rate {m['achieved_hz']:g} < {lo:g} Hz"))
        if m['dq'] == 'zero':
            issues.append((FAIL, f"bad data: {m['dq_note']}"))
        elif m['dq'] == 'flat':
            issues.append((WARN, f"suspect data: {m['dq_note']}"))
    v = OK
    for lvl, _ in issues:
        if _RANK[lvl] > _RANK[v]:
            v = lvl
    m['issues'], m['verdict'] = issues, v
    return m


# ── reporting ─────────────────────────────────────────────────────────────────

_ICON = {OK: '✅', WARN: '⚠️ ', FAIL: '❌'}


def render_report(metrics, args):
    lines = ['', '═' * 74, '  SENSOR SMOKE CHECK — report card', '═' * 74,
             f"  {'':<2}{'sensor':<9}{'tier':<5}{'open':<6}{'Hz':>7} {'band':>12}"
             f"  {'stab':>5} {'gap':>5}  {'data':<6} verdict",
             '  ' + '-' * 70]
    for m in metrics:
        band = m['band']
        band_s = f"{band[0]:g}-{band[2]:g}" if band else '—'
        hz = f"{m['achieved_hz']:.1f}" if m['streaming'] else '—'
        stab = f"{m['cov']*100:.0f}%" if m['streaming'] else '—'
        gap = f"{m['max_gap']:.1f}s" if m['streaming'] else '—'
        openf = ('yes' if m['opened'] else 'NO')
        lines.append(
            f"  {_ICON[m['verdict']]} {m['name'][:9]:<9}{m['tier']:<5}{openf:<6}"
            f"{hz:>7} {band_s:>12}  {stab:>5} {gap:>5}  {m['dq']:<6}{m['verdict']}")
    lines.append('  ' + '-' * 70)
    # per-sensor detail
    for m in metrics:
        if m['issues']:
            lines.append(f"  {m['name']}:")
            for lvl, text in m['issues']:
                lines.append(f"    {_ICON[lvl]} {text}")
    # pair co-run sync (two live sensors only)
    pair = [m for m in metrics if m['name'] != 'audio']
    if len(pair) == 2:
        lines += _render_pair_sync(pair, args)
    # overall
    worst = OK
    for m in metrics:
        if _RANK[m['verdict']] > _RANK[worst]:
            worst = m['verdict']
    lines.append('  ' + '═' * 70)
    if worst == OK:
        verdict = "✅ all checks PASSED — safe to add to the cluster"
    elif worst == WARN:
        verdict = "⚠️  PASSED WITH WARNINGS — fix before trusting the synced dataset"
    else:
        verdict = "❌ FAILED — do NOT add to the cluster until fixed"
    lines.append(f"  OVERALL: {verdict}")
    lines.append('═' * 74)
    return '\n'.join(lines), worst


def _render_pair_sync(pair, args):
    a, b = pair
    out = ['', f"  ── co-run sync: {a['name']} + {b['name']} ──"]
    if not (a['streaming'] and b['streaming']):
        out.append("    ⚠️  one stream isn't live — sync check skipped")
        return out
    skew = (abs(a['first_t'] - b['first_t'])
            if (a['first_t'] is not None and b['first_t'] is not None) else None)
    # simultaneous liveness across the aligned poll window
    global _LAST_POLLS
    both, total = 0, 0
    if _LAST_POLLS:
        pa, pb = a['name'], b['name']
        prev = {}
        for row in _LAST_POLLS:
            if row['t'] < args.settle:
                continue
            live_now = {}
            for nm in (pa, pb):
                kind, val, _s, _m = row[nm]
                if kind == 'rate':
                    live_now[nm] = val > 0
                else:
                    live_now[nm] = (nm in prev) and (val > prev[nm])
                    prev[nm] = val
            total += 1
            if live_now.get(pa) and live_now.get(pb):
                both += 1
    co = (both / total) if total else 0.0
    if skew is not None:
        s_icon = '✅' if skew < 0.5 else '⚠️ '
        out.append(f"    {s_icon} start skew: {skew*1000:.0f} ms "
                   f"(first real sample of each)")
    c_icon = '✅' if co >= 0.8 else '⚠️ '
    out.append(f"    {c_icon} both live together: {co*100:.0f}% of the window")
    ok = (a['verdict'] != FAIL and b['verdict'] != FAIL
          and (skew is None or skew < 0.5) and co >= 0.8)
    out.append("    → " + ("✅ this pair runs cleanly together — safe to cluster"
                           if ok else
                           "⚠️  resolve the issues above before running these together"))
    return out


_LAST_POLLS = None


# ── main ──────────────────────────────────────────────────────────────────────

def _list_modalities():
    print("Testable modalities (--only <name[,name]>):\n")
    print(f"  {'name':<10}{'tier':<6}{'expected Hz':<14}what it checks")
    print('  ' + '-' * 60)
    for n in TESTABLE:
        mod = BY_ID[SENSOR_TO_MODALITY[n]]
        hz = f"{mod.expected_hz[0]:g}-{mod.expected_hz[2]:g}" if mod.expected_hz else '—'
        print(f"  {n:<10}{mod.tier:<6}{hz:<14}{mod.label}")
    print("\n  tiers: A=sub-frame/onset  B=~100ms  C=seconds")


def build_parser():
    p = argparse.ArgumentParser(
        prog='python -m src.tools.sensor_check',
        description='Smoke-test 1 or 2 modalities in isolation before clustering.')
    p.add_argument('--only', type=str,
                   help='comma list of 1-2 modalities: ' + ', '.join(TESTABLE))
    p.add_argument('--list', action='store_true', help='list modalities and exit')
    p.add_argument('-t', '--duration', type=float, default=12.0,
                   help='seconds to observe each stream (default 12)')
    p.add_argument('--settle', type=float, default=2.0,
                   help='warm-up seconds ignored in the rate stats (default 2)')
    p.add_argument('--poll', type=float, default=0.2, help='poll interval s')
    p.add_argument('--stall', type=float, default=3.0,
                   help='no-sample seconds that counts as a stall (default 3)')
    p.add_argument('--no-live', action='store_true', help='no live board, report only')
    # device config (same defaults as the dashboard)
    p.add_argument('--camera-source', default='auto')
    p.add_argument('--cam-res', default='1280x720')
    p.add_argument('--oxi-port', default='auto')
    p.add_argument('--csi-port', default='auto')
    p.add_argument('--csi-baud', type=int, default=115200)
    p.add_argument('--emg-port', default='auto')
    p.add_argument('--emg-baud', type=int, default=230400)
    p.add_argument('--gsr-port', default='auto')
    p.add_argument('--gsr-baud', type=int, default=115200)
    p.add_argument('--thermal-source', default='auto')
    p.add_argument('--audio-device', default=None)
    return p


def main(argv=None):
    global _LAST_POLLS
    args = build_parser().parse_args(argv)
    if args.list or not args.only:
        _list_modalities()
        return 0 if args.list else 2

    names = [s.strip() for s in args.only.split(',') if s.strip()]
    bad = [n for n in names if n not in TESTABLE]
    if bad:
        print(f"unknown modality: {', '.join(bad)}\n")
        _list_modalities()
        return 2
    if len(names) > 2:
        print(f"note: {len(names)} selected — this tool is for isolating 1-2 at a "
              f"time; running all anyway.\n")
    if args.settle >= args.duration:
        args.settle = max(0.0, args.duration * 0.2)

    print(f"\n▶ smoke check: {', '.join(names)}  ({args.duration:.0f}s)")

    metrics = []
    live = [n for n in names if n != 'audio']
    registry = SensorRegistry()
    sio = CaptureSio()
    ERRORS.clear()
    orch = None
    handlers = {}
    try:
        orch = maybe_assign_ports(live, args, registry, sio)
        for n in live:
            try:
                handlers[n] = build_handler(n, args, registry, sio)
            except Exception as e:
                ERRORS[n] = f'{type(e).__name__}: {e}'
                print(f"  · {n}: cannot build handler — {ERRORS[n]}")

        polls = []
        if handlers:
            polls, t0 = monitor(live, handlers, registry, args)
        else:
            t0 = time.monotonic()
        _LAST_POLLS = polls

        for n in live:
            if n in handlers or n in ERRORS:
                metrics.append(compute_metrics(n, polls, sio, t0, args)
                               if n in handlers else _broken_metric(n))
    finally:
        if orch is not None:
            try:
                orch.stop()
            except Exception:
                pass

    if 'audio' in names:
        metrics.append(check_audio(args))

    # keep the report in the user's requested order
    order = {n: i for i, n in enumerate(names)}
    metrics.sort(key=lambda m: order.get(m['name'], 99))
    report, worst = render_report(metrics, args)
    print(report)
    return 0 if worst == OK else 1


def _broken_metric(name):
    band = BY_ID[SENSOR_TO_MODALITY[name]].expected_hz
    m = dict(name=name, tier=BY_ID[SENSOR_TO_MODALITY[name]].tier, band=band,
             opened=False, streaming=False, achieved_hz=0.0, cov=0.0, max_gap=0.0,
             first_t=None, state='build-failed', msg=ERRORS.get(name, ''),
             dq='na', dq_n=0, dq_note='', error=ERRORS.get(name))
    m['issues'] = [(FAIL, f"handler could not be built: {m['error']}")]
    m['verdict'] = FAIL
    return m


if __name__ == '__main__':
    raise SystemExit(main())
