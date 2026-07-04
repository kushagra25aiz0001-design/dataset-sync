"""Mux helper: offset math + first-real-frame parsing (pure stdlib)."""
import csv, os, sys, tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.preprocessing.mux_av import av_offset, first_real_frame_ts


def check(name, cond, extra=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}  {extra}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    # audio started 0.6 s after the first video frame → delay audio by +0.6
    check('audio later → positive delay', av_offset(0.05, 0.65) == 0.6)
    # audio started before video → negative (trim head)
    check('audio earlier → negative', round(av_offset(1.0, 0.2), 3) == -0.8)

    # first_real_frame_ts skips FRC duplicates (is_real==0)
    d = tempfile.mkdtemp()
    p = os.path.join(d, 'timestamps.csv')
    with open(p, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['frame_idx', 'timestamp_s', 'filename', 'is_real'])
        w.writerow([0, '0.0000', 'f0', 0])      # FRC duplicate — skip
        w.writerow([1, '0.0332', 'f1', 1])      # first real frame
        w.writerow([2, '0.0664', 'f2', 1])
    check('first real frame ts', abs(first_real_frame_ts(p) - 0.0332) < 1e-6)

    # older schema without is_real → first row
    p2 = os.path.join(d, 't2.csv')
    with open(p2, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(['frame_idx', 'timestamp_s', 'filename'])
        w.writerow([0, '0.0100', 'f0'])
    check('older schema → first row', abs(first_real_frame_ts(p2) - 0.01) < 1e-6)
    check('missing file → None', first_real_frame_ts('/nope/x.csv') is None)

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
