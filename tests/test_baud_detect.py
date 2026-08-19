"""Baud detector scoring core: distinguish real CSI text from wrong-baud garbage."""
import os, random, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.recorder.baud_detect import score_serial_text, rank


def csi_line(ts):
    # timestamp, seq, rssi, n_carriers(128), then 128 amplitudes
    amps = ','.join(str(random.randint(0, 90)) for _ in range(128))
    return f'{ts},{ts+7},-{random.randint(50,70)},128,{amps}'


def check(name, cond, extra=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}  {extra}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    random.seed(1)

    # ── real CSI text (correct baud) ──
    good = ('\n'.join(csi_line(11548000 + i * 33) for i in range(40)) + '\n').encode()
    s = score_serial_text(good)
    check('CSI text: many CSI lines', s['n_csi_lines'] >= 35, f"{s['n_csi_lines']}")
    check('CSI text: high printable ratio', s['printable_ratio'] > 0.95)
    check('CSI text: sample captured', ',128,' in s['sample'])

    # ── wrong baud → mostly non-printable garbage ──
    garbage = bytes(random.randint(0, 255) for _ in range(8000))
    g = score_serial_text(garbage)
    check('garbage: ~no CSI lines', g['n_csi_lines'] == 0, f"{g['n_csi_lines']}")
    check('garbage: low printable ratio', g['printable_ratio'] < 0.6,
          f"{g['printable_ratio']}")

    # ── printable but not CSI (e.g. some other device's log lines) ──
    other = ('\n'.join('boot: heap %d free' % (i * 100) for i in range(50)) + '\n').encode()
    o = score_serial_text(other)
    check('non-CSI text: no CSI lines', o['n_csi_lines'] == 0)
    check('non-CSI text: printable though', o['printable_ratio'] > 0.95)

    # ── empty ──
    check('empty input safe', score_serial_text(b'')['n_csi_lines'] == 0)

    # ── ranking picks the CSI baud over garbage/other ──
    results = [
        {'baud': 9600, **score_serial_text(garbage)},
        {'baud': 115200, **score_serial_text(good)},
        {'baud': 74880, **score_serial_text(other)},
        {'baud': 460800, 'error': 'could not open port'},
    ]
    best = rank(results)
    check('rank picks the CSI baud', best is not None and best['baud'] == 115200,
          f"{best and best['baud']}")
    check('rank returns None when nothing is CSI',
          rank([{'baud': 9600, **score_serial_text(garbage)}]) is None)

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
