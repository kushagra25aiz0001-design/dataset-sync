"""Post-hoc ECG alignment: recover a known device offset + drift by matching an
ECG beat series (Frontier X clock) against a PPG beat series (master clock).
Pure logic — no numpy/hardware."""
import json, math, os, random, sys, tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.preprocessing.ecg_align import (
    align_beats, estimate_lag, remap_times,
    parse_wallclock, map_wall_to_master, match_ecg_to_session)


def synth_heart(seed=1, t0=5.0, t1=185.0):
    """True beat times on the master clock, with realistic *aperiodic* HRV — a
    random-walk RR baseline (LF) plus beat-to-beat noise (HF). This broadband
    fingerprint is non-repeating, which is what lets the cross-correlation lock
    onto a single, unambiguous lag (a clean periodic HR would have side-lobes)."""
    random.seed(seed)
    beats, t, rr = [], t0, 0.85
    while t < t1:
        beats.append(t)
        rr = min(1.2, max(0.55, rr + random.gauss(0, 0.02)))   # random-walk baseline
        t += rr + random.gauss(0, 0.015)                       # + HF noise
    return beats


def check(name, cond, extra=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}  {extra}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    random.seed(7)
    M = synth_heart()

    # PPG beats on the master clock: small detection jitter + a few dropped beats.
    ppg = [m + random.gauss(0, 0.006) for m in M if random.random() > 0.03]

    # Frontier X ECG on its own clock: t_master = a·t_device + b.
    A_TRUE, B_TRUE = 1.0 + 50e-6, 37.11        # 50 ppm drift, 37.11 s offset
    ecg = [(m - B_TRUE) / A_TRUE + random.gauss(0, 0.004)
           for m in M if random.random() > 0.02]

    # ── full alignment ──
    res = align_beats(ecg, ppg)
    check('locked', res is not None)
    check('offset recovered', abs(res['offset_s'] - B_TRUE) < 0.05,
          f"got {res['offset_s']:.4f} vs {B_TRUE}")
    check('drift recovered (±30 ppm)', abs(res['drift_ppm'] - 50) < 30,
          f"got {res['drift_ppm']} ppm")
    check('residual small', res['residual_ms'] < 20, f"{res['residual_ms']} ms")
    check('most beats matched', res['match_rate'] > 0.8, f"{res['match_rate']}")

    # ── remap moves ECG samples onto the master clock ──
    # a device sample at device-time d should land at a·d + b.
    d = 100.0
    mapped = remap_times([d], res['a'], res['b'])[0]
    expected = A_TRUE * d + B_TRUE
    check('remap places sample on master clock', abs(mapped - expected) < 0.05,
          f"{mapped:.3f} vs {expected:.3f}")

    # ── near-steady HR (weak HRV): prior seeding (a chest-tap marker) helps ──
    random.seed(3)
    tt, steady = 0.0, []
    while tt < 180:                                   # ~flat 75 bpm + faint HRV
        steady.append(tt)
        tt += 0.80 + random.gauss(0, 0.01)
    steady_ppg = [s + 5.0 + random.gauss(0, 0.004) for s in steady]  # offset 5.0 s
    seeded = estimate_lag(steady, steady_ppg, prior_offset_s=5.0, prior_window_s=3.0)
    check('prior-seeded lag near truth', seeded is not None
          and abs(seeded['offset_s'] - 5.0) < 0.3, f"{seeded}")

    # ── refuses when there is no real overlap ──
    none_case = align_beats([1, 2, 3, 4, 5], [1000, 1001, 1002, 1003], max_lag_s=10)
    check('refuses to lock without overlap', none_case is None)

    # ── wall-clock coarse alignment (Frontier X report Start Time ↔ session) ──
    check('parse report wallclock',
          parse_wallclock('2026-06-11 18:47:15').hour == 18)
    check('parse metadata ISO+tz (drops tz)',
          parse_wallclock('2026-06-11T18:47:21.690154+05:30').second == 21)
    # ECG began 6.69 s before the recorder session (matches the real data).
    off = map_wall_to_master('2026-06-11 18:47:15', '2026-06-11T18:47:21.690154+05:30')
    check('ECG start maps to master ≈ -6.69 s', abs(off - (-6.69)) < 0.01, f'{off:.3f}')

    # match_ecg_to_session against a temp session metadata
    root = tempfile.mkdtemp(prefix='ecgmatch_')
    sd = os.path.join(root, 'session_20260611_184721'); os.makedirs(sd)
    json.dump({'session_id': 'session_20260611_184721',
               'start': '2026-06-11T18:47:21.690154+05:30', 'duration_target': 300},
              open(os.path.join(sd, 'metadata.json'), 'w'))
    m = match_ecg_to_session('2026-06-11 18:47:15', '2026-06-11 18:52:15', raw_root=root)
    check('matched ECG report to session',
          m is not None and m['session_id'] == 'session_20260611_184721')
    check('overlap ≈ 293 s', abs(m['overlap_s'] - 293.3) < 1.0, f"{m['overlap_s']}")
    # a report from a different hour must NOT match this session
    none_m = match_ecg_to_session('2026-06-11 12:00:00', '2026-06-11 12:05:00', raw_root=root)
    check('no false match for distant time', none_m is None)

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
