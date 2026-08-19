"""Tests for the Polar H10 ECG recorder: PMD frame parsing and master-clock
sample anchoring. No BLE stack / hardware required (only the pure logic)."""
import csv, os, sys, tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.recorder.ecg_recorder import (
    parse_ecg_frame, PolarH10ECGRecorder, ECG_SAMPLE_HZ)


def make_frame(device_ts_ns, samples_uV):
    """Build a synthetic PMD ECG notification (type 0x00, int24 LE samples)."""
    b = bytearray([0x00])
    b += int(device_ts_ns).to_bytes(8, 'little')
    b += bytes([0x00])                          # frame type 0
    for v in samples_uV:
        b += int(v).to_bytes(3, 'little', signed=True)
    return bytes(b)


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    # ── 1. Round-trip parse, including negative int24 ──
    samples = [0, 1, -1, 2047, -2048, 123456, -123456]
    frame = make_frame(1234567890123456789, samples)
    ts, ftype, got = parse_ecg_frame(frame)
    check('device_ts parsed', ts == 1234567890123456789)
    check('frame type 0', ftype == 0)
    check('samples round-trip (signed int24)', got == samples)
    check('non-ECG frame rejected', parse_ecg_frame(b'\x01\x00\x00') is None)
    check('short frame rejected', parse_ecg_frame(b'\x00\x00') is None)

    # ── 2. Master-clock anchoring: two batches → contiguous 130 Hz timeline ──
    root = tempfile.mkdtemp(prefix='ecg_')
    fake_now = [1000.0]                          # controllable monotonic clock
    import src.recorder.ecg_recorder as mod
    orig = mod.time.monotonic
    mod.time.monotonic = lambda: fake_now[0]
    try:
        rec = PolarH10ECGRecorder(root, t0=990.0)   # rec_start = 990 → origin
        # open the CSV writer without starting BLE
        rec._csv_f = open(os.path.join(root, 'ecg_log.csv'), 'w', newline='')
        rec._csv_w = csv.writer(rec._csv_f)
        rec._csv_w.writerow(['timestamp_s', 'ecg_uV', 'device_ts_ns'])

        fake_now[0] = 1000.0                     # first batch received at t=10s master
        rec._on_ecg(None, make_frame(111, list(range(5))))   # 5 samples
        fake_now[0] = 1000.5
        rec._on_ecg(None, make_frame(222, list(range(5, 10))))  # 5 more
        rec._csv_f.close()
    finally:
        mod.time.monotonic = orig

    rows = list(csv.DictReader(open(os.path.join(root, 'ecg_log.csv'))))
    check('all 10 samples written', len(rows) == 10)
    ts0 = float(rows[0]['timestamp_s'])
    # sample 0 anchored to (10s - (5-1)/130)
    check('first sample anchored on master clock',
          abs(ts0 - (10.0 - 4 / ECG_SAMPLE_HZ)) < 1e-6)
    # Samples form a contiguous 130 Hz grid across both batches (no BLE jitter in
    # timestamps). Tolerance = 1 LSB of the 5-decimal CSV (±1e-5 s), far below the
    # 7.7 ms ECG sample period.
    dt = [float(rows[i + 1]['timestamp_s']) - float(rows[i]['timestamp_s'])
          for i in range(9)]
    check('contiguous ~1/130 s spacing (no BLE jitter in timestamps)',
          all(abs(d - 1 / ECG_SAMPLE_HZ) < 1.1e-5 for d in dt))
    check('values preserved across batches',
          [int(r['ecg_uV']) for r in rows] == list(range(10)))
    check('device_ts carried per batch',
          rows[0]['device_ts_ns'] == '111' and rows[9]['device_ts_ns'] == '222')

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
