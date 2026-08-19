"""Session recovery: detect + finalize a crashed session without losing data."""
import csv, json, os, sys, tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.recorder.session_recovery import (
    looks_interrupted, scan_interrupted, finalize_interrupted)


def _csv(path, header, n, ts_step):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(header)
        for i in range(n):
            w.writerow([f'{i*ts_step:.4f}'] + [i % 10] * (len(header) - 1))


def check(name, cond, extra=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}  {extra}")
    if not cond:
        check.failed += 1
check.failed = 0


def build_crashed(root):
    sd = os.path.join(root, 'session_20260703_120000')
    os.makedirs(sd)
    # data on disk (handlers appended fine) up to ~30 s
    _csv(os.path.join(sd, 'oximeter', 'oximeter_log.csv'),
         ['timestamp_s', 'spo2', 'heart_rate', 'signal_strength', 'pleth'],
         1800, 1 / 60)                     # 60 Hz * 30 s
    _csv(os.path.join(sd, 'camera', 'timestamps.csv'),
         ['frame_idx', 'timestamp_s', 'filename', 'is_real'], 900, 1 / 30)  # 30 Hz
    # markers.csv with session_start but NO session_end (crash)
    with open(os.path.join(sd, 'markers.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['t_master_s', 'label', 'source', 't_device_s', 'offset_s', 'payload_json'])
        w.writerow([0.01, 'session_start', 'backend', '', '', ''])
    # metadata WITHOUT duration_actual / stats (stop_recording never ran)
    json.dump({'session_id': 'session_20260703_120000', 'subject': 'S01',
               'start': '2026-07-03T12:00:00+05:30', 'duration_target': 300},
              open(os.path.join(sd, 'metadata.json'), 'w'))
    return sd


def main():
    root = tempfile.mkdtemp(prefix='recovery_')
    sd = build_crashed(root)

    check('detected as interrupted', looks_interrupted(sd) is True)
    check('scan finds it', sd in scan_interrupted(root))

    meta = finalize_interrupted(sd)
    check('recovered flag set', meta.get('recovered') is True)
    check('duration reconstructed ≈ 29.98 s',
          abs(meta['duration_actual'] - (899 / 30)) < 0.1
          or abs(meta['duration_actual'] - (1799 / 60)) < 0.1,
          f"{meta['duration_actual']}")
    check('oxi row count', meta['stats']['oxi'] == 1800, f"{meta['stats']['oxi']}")
    check('cam row count', meta['stats']['cam'] == 900)
    check('session_end appended', meta['recovered_session_end'] is True)

    # markers.csv now closed with session_end
    labels = [r[1] for r in csv.reader(open(os.path.join(sd, 'markers.csv')))][1:]
    check('markers.csv has session_end', 'session_end' in labels)

    # after finalize it is no longer interrupted, and re-running is safe
    check('no longer interrupted', looks_interrupted(sd) is False)
    meta2 = finalize_interrupted(sd)
    check('idempotent: no duplicate session_end',
          meta2['recovered_session_end'] is False
          and [r[1] for r in csv.reader(open(os.path.join(sd, 'markers.csv')))].count('session_end') == 1)

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
