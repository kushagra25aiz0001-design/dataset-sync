"""Sensor smoke-checker metric logic: the verdicts that decide whether a modality
is safe to add to the cluster. Pure logic — fabricated poll series + a fake sio,
no hardware. Locks in the failure modes that silently desync the real dataset:
a dead stream, a stall, a too-slow (lagging) stream, corrupt/flat data, and the
EMG *mock feed* (socket data with no real samples)."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import src.tools.sensor_check as sc


def args(duration=6.0, settle=1.0, poll=0.2, stall=3.0):
    return types.SimpleNamespace(duration=duration, settle=settle, poll=poll,
                                 stall=stall, no_live=True)


def build_polls(name, kind, values, state='streaming', msg='port @ baud'):
    """values: per-poll cumulative counts (kind='count') or fps (kind='rate')."""
    return [{'t': i * 0.2, name: (kind, float(v), state, msg)}
            for i, v in enumerate(values)]


def gsr_sio(vals):
    """Fake sio carrying gsr_data payloads; `vals` are per-event uS values."""
    sio = sc.CaptureSio()
    t0 = time.monotonic()
    for i, v in enumerate(vals):
        sio.data['gsr_data'].append((t0 + 0.05 + i * 0.2,
                                     {'uS': v, 'raw': 1800, 'n': i}))
    return sio, t0


def check(name, cond, extra=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}  {extra}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    A = args()

    # ── steady in-band stream → PASS ──
    polls = build_polls('gsr', 'count', [10 * i for i in range(30)])   # 50 Hz
    sio, t0 = gsr_sio([2.0 + 0.3 * (i % 5) for i in range(30)])
    sc._LAST_POLLS = polls
    m = sc.compute_metrics('gsr', polls, sio, t0, A)
    check('steady stream verdict PASS', m['verdict'] == 'PASS', m['verdict'])
    check('achieved Hz ≈ 50', 45 <= m['achieved_hz'] <= 55, f"{m['achieved_hz']:.1f}")
    check('data quality ok', m['dq'] == 'ok', m['dq'])

    # ── too slow (below the band's min) → WARN 'slow' (the lag that desyncs) ──
    polls = build_polls('gsr', 'count', [1 * i for i in range(30)])    # 5 Hz < 10
    sio, t0 = gsr_sio([2.0 + 0.3 * (i % 5) for i in range(30)])
    m = sc.compute_metrics('gsr', polls, sio, t0, A)
    check('slow stream is WARN', m['verdict'] == 'WARN', m['verdict'])
    check('slow reason mentions Hz', any('slow' in t for _, t in m['issues']))

    # ── stall: advances then freezes for >stall seconds → FAIL ──
    vals = [10 * i for i in range(11)] + [100] * 19                    # frozen after t≈2s
    polls = build_polls('gsr', 'count', vals)
    sio, t0 = gsr_sio([2.0 + 0.3 * (i % 5) for i in range(11)])
    m = sc.compute_metrics('gsr', polls, sio, t0, A)
    check('stalled stream is FAIL', m['verdict'] == 'FAIL', m['verdict'])
    check('stall detected in gap', m['max_gap'] >= A.stall, f"gap={m['max_gap']:.1f}")

    # ── opened but zero real samples (counter never moves) → FAIL, data n/a ──
    polls = build_polls('gsr', 'count', [0] * 30, state='streaming')
    sio, t0 = gsr_sio([])
    m = sc.compute_metrics('gsr', polls, sio, t0, A)
    check('dead stream is FAIL', m['verdict'] == 'FAIL', m['verdict'])
    check('dead stream not streaming', not m['streaming'])
    check('dead stream data n/a (not trusted)', m['dq'] == 'na', m['dq'])

    # ── corrupt data: NaN in payload → FAIL 'bad data' ──
    polls = build_polls('gsr', 'count', [10 * i for i in range(30)])
    sio, t0 = gsr_sio([float('nan')] * 30)
    m = sc.compute_metrics('gsr', polls, sio, t0, A)
    check('NaN data is FAIL', m['verdict'] == 'FAIL' and m['dq'] == 'nan', m['dq'])

    # ── flat-line data: constant value → WARN 'flat' ──
    polls = build_polls('gsr', 'count', [10 * i for i in range(30)])
    sio, t0 = gsr_sio([3.14] * 30)
    m = sc.compute_metrics('gsr', polls, sio, t0, A)
    check('flat-line data is WARN', m['verdict'] == 'WARN' and m['dq'] == 'flat', m['dq'])

    # ── EMG mock feed: state=error, counter stuck at 0, 'mock' in status → FAIL,
    #    and the fabricated feed is called out (never trusted as real). ──
    polls = build_polls('emg', 'count', [0] * 20, state='error',
                        msg='[Errno 2] could not open port')
    sio = sc.CaptureSio()
    tm = time.monotonic()
    sio.status['emg'].append((tm, False, '/dev/ttyUSB0 (Running Mock Feed)'))
    sio.data['emg_data'].append((tm, {'channels': [2048, 2100, 1990], 'n': 10}))
    m = sc.compute_metrics('emg', polls, sio, tm, A)
    check('emg mock feed is FAIL', m['verdict'] == 'FAIL', m['verdict'])
    check('emg mock feed not streaming', not m['streaming'])
    check('emg mock flagged synthetic',
          any('mock' in t.lower() or 'synthetic' in t.lower() for _, t in m['issues']))

    # ── rate-sourced (camera) steady 30 fps in band → PASS ──
    polls = build_polls('camera', 'rate', [30.0] * 30, state='streaming')
    m = sc.compute_metrics('camera', polls, sio, time.monotonic(), A)
    check('camera 30fps PASS', m['verdict'] == 'PASS', m['verdict'])
    check('camera data is visual n/a', m['dq'] == 'na', m['dq'])

    # ── pair co-run sync: two good streams starting together → safe ──
    good = []
    for i in range(30):
        good.append({'t': i * 0.2,
                     'gsr': ('count', float(10 * i), 'streaming', ''),
                     'camera': ('rate', 30.0, 'streaming', '')})
    sc._LAST_POLLS = good
    sio2, t0b = gsr_sio([2.0 + 0.3 * (i % 5) for i in range(30)])
    mg = sc.compute_metrics('gsr', good, sio2, t0b, A)
    mc = sc.compute_metrics('camera', good, sio2, t0b, A)
    report, worst = sc.render_report([mg, mc], A)
    check('pair report overall PASS', worst == 'PASS', worst)
    check('pair marked safe to cluster', 'safe to cluster' in report)
    check('report renders a card row', 'report card' in report)

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
