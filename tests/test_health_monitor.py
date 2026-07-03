"""HealthMonitor: per-sensor Hz + stall detection from health snapshots."""
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.session.health_monitor import HealthMonitor, render_board


def snap(recording=True, **counts):
    return {'recording': recording, 'session_id': 'session_x',
            'sensors': {n: {'count': c, 'ok': True, 'state': 'streaming'}
                        for n, c in counts.items()}}


def check(name, cond, extra=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}  {extra}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    hm = HealthMonitor(min_hz={'oxi': 30, 'cam': 25}, stall_after_s=3.0, ema=1.0)

    # t=0 first snapshot → no rate yet
    b0 = hm.update(snap(oxi=0, cam=0), now=0.0)
    check('first update hz=0', b0['sensors']['oxi']['hz'] == 0.0)

    # t=1 oxi advanced 60, cam advanced 30 → 60 Hz / 30 Hz
    b1 = hm.update(snap(oxi=60, cam=30), now=1.0)
    check('oxi 60 Hz', b1['sensors']['oxi']['hz'] == 60.0, f"{b1['sensors']['oxi']['hz']}")
    check('cam 30 Hz', b1['sensors']['cam']['hz'] == 30.0)
    check('overall ok', b1['overall'] == 'ok')

    # t=2 cam barely advances (5 in 1s = 5 Hz < min 25) → degraded
    b2 = hm.update(snap(oxi=120, cam=35), now=2.0)
    check('cam low-rate flagged', b2['sensors']['cam']['status'] == 'low')
    check('overall degraded', b2['overall'] == 'degraded')

    # cam counter freezes → stall after 3 s
    hm.update(snap(oxi=180, cam=35), now=3.0)
    hm.update(snap(oxi=240, cam=35), now=4.0)
    b5 = hm.update(snap(oxi=300, cam=35), now=5.5)   # 3.5 s since cam last advanced
    check('cam stalled', b5['sensors']['cam']['stalled'] is True)
    check('overall stalled', b5['overall'] == 'stalled')
    check('oxi still fine while cam stalls', b5['sensors']['oxi']['status'] == 'ok')

    # renderer produces a usable board
    txt = render_board(b5, elapsed_s=125)
    check('render mentions sensors + verdict',
          'oxi' in txt and 'cam' in txt and 'STALL' in txt and '02:05' in txt)

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
