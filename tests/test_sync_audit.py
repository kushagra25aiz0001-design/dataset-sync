"""Synthetic-session tests for the sync audit — validates the verdict logic
against fabricated sessions with known-good and known-bad timing."""
import csv, json, os, struct, sys, tempfile, wave

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.preprocessing.sync_audit import SessionSyncAudit


def _w(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as f:
        wr = csv.writer(f); wr.writerow(header)
        for r in rows:
            wr.writerow(r)


def _ts_stream(path, header, rate, dur, cols=1, t0=0.0, real=None):
    n = int(rate * dur)
    rows = []
    for i in range(n):
        t = t0 + i / rate
        row = [f'{t:.4f}'] + [i % 100] * cols
        if real is not None:
            row.append(real(i))
        rows.append(row)
    _w(path, header, rows)


def build_good(sd):
    """A clean session with all captured modalities on the master clock."""
    os.makedirs(sd, exist_ok=True)
    # facial video @30, with is_real (every 5th frame an FRC duplicate)
    n = 30 * 20
    rows = []
    for i in range(n):
        real = 0 if i % 5 == 4 else 1
        t = i / 30.0 if real else (i / 30.0)
        rows.append([i, f'{t:.4f}', f'f{i}', real])
    _w(os.path.join(sd, 'camera', 'timestamps.csv'),
       ['frame_idx', 'timestamp_s', 'filename', 'is_real'], rows)
    # oximeter @60 with pleth
    _ts_stream(os.path.join(sd, 'oximeter', 'oximeter_log.csv'),
               ['timestamp_s', 'spo2', 'heart_rate', 'signal_strength', 'pleth'],
               60, 20, cols=4)
    # emg @1000
    _ts_stream(os.path.join(sd, 'emg', 'emg_log.csv'),
               ['timestamp_s'] + [f'ch{i}' for i in range(3)], 1000, 20, cols=3)
    # gsr @50
    _ts_stream(os.path.join(sd, 'gsr', 'gsr_log.csv'),
               ['timestamp_s', 'uS', 'raw', 'stress', 'zscore'], 50, 20, cols=4)
    # ecg @250
    _ts_stream(os.path.join(sd, 'ecg', 'ecg_log.csv'),
               ['timestamp_s', 'mV'], 250, 20, cols=1)
    # thermal @25 (frame-style: frame_idx, timestamp_s, filename, is_real)
    rows = [[i, f'{i/25.0:.4f}', f't{i}', 1] for i in range(25 * 20)]
    _w(os.path.join(sd, 'thermal', 'timestamps.csv'),
       ['frame_idx', 'timestamp_s', 'filename', 'is_real'], rows)
    # CSI: anchor file with pc_timestamp_s + raw_line (device ms drifting 50ppm)
    rows = []
    for i in range(60 * 20):
        pc = i / 60.0
        dev_ms = pc * 1000.0 * (1 + 50e-6)  # 50 ppm drift
        raw_line = f'{dev_ms:.1f},{i},-60,128,' + ','.join(['30'] * 128)
        rows.append([f'{pc:.4f}', raw_line])
    _w(os.path.join(sd, 'csi', 'csi_timestamped.csv'),
       ['pc_timestamp_s', 'raw_line'], rows)
    _w(os.path.join(sd, 'csi', 'csi_log.csv'), None or ['x'], [])
    # audio: real WAV + meta anchored at 0.05s
    ad = os.path.join(sd, 'audio'); os.makedirs(ad, exist_ok=True)
    sr, dur = 16000, 20
    with wave.open(os.path.join(ad, 'audio.wav'), 'wb') as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(b'\x00\x00' * (sr * dur))
    with open(os.path.join(ad, 'audio_meta.json'), 'w') as f:
        json.dump({'t_start_master_s': 0.05, 'samplerate': sr,
                   'channels': 1, 'n_frames': sr * dur}, f)
    # markers: session + a questionnaire rating + a BP reading
    _w(os.path.join(sd, 'markers.csv'),
       ['t_master_s', 'label', 'source', 't_device_s', 'offset_s', 'payload_json'],
       [[0.01, 'session_start', 'backend', '', '', ''],
        [3.0, 'rating:SAM:pic1', 'backend', '', '', '{"valence":5}'],
        [5.0, 'rating:BP:rest', 'backend', '', '', '{"sys":120,"dia":80}'],
        [10.0, 'response:arith:1', 'backend', '', '', '{"correct":true}']])


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    root = tempfile.mkdtemp(prefix='syncaudit_')
    out = os.path.join(root, 'processed')

    # ── 1. All-good session → PASS, every captured stream in sync ──
    sd = os.path.join(root, 'data', 'raw', 'session_good')
    build_good(sd)
    rep = SessionSyncAudit(sd, out_root=out).run()
    print(SessionSyncAudit(sd, out_root=out).render(rep))
    M = rep['modalities']
    check('overall PASS', rep['overall'] == 'PASS')
    for mid in ['facial_video', 'oximeter', 'emg', 'gsr', 'ecg', 'thermal', 'wifi_csi', 'audio']:
        check(f'{mid} in_sync', M[mid]['verdict'] == 'in_sync')
    check('questionnaire in_sync', M['questionnaire']['verdict'] == 'in_sync')
    check('bp in_sync', M['bp']['verdict'] == 'in_sync')
    check('rppg derived (follows camera)', M['rppg']['verdict'] == 'derived')
    check('csi drift ~50ppm detected',
          abs(abs(M['wifi_csi']['checks']['clock_fit']['drift_ppm']) - 50) < 5)
    check('camera FRC frac ~0.2',
          abs(M['facial_video']['checks']['frc_duplicate_frac'] - 0.2) < 0.05)
    check('cross-modal overlap positive', rep['cross_modal']['overlap_s'] > 15)

    # ── 2. Foreign-clock EMG (wall-clock epoch) → out_of_sync + overall FAIL ──
    sd2 = os.path.join(root, 'data', 'raw', 'session_epoch')
    build_good(sd2)
    _ts_stream(os.path.join(sd2, 'emg', 'emg_log.csv'),
               ['timestamp_s', 'ch0'], 1000, 20, cols=1, t0=1.75e9)  # epoch!
    rep2 = SessionSyncAudit(sd2, out_root=out).run()
    check('epoch EMG out_of_sync', rep2['modalities']['emg']['verdict'] == 'out_of_sync')
    check('epoch session overall FAIL', rep2['overall'] == 'FAIL')

    # ── 3. Late-start GSR (starts 12s in) → degraded + overall WARN ──
    sd3 = os.path.join(root, 'data', 'raw', 'session_late')
    build_good(sd3)
    _ts_stream(os.path.join(sd3, 'gsr', 'gsr_log.csv'),
               ['timestamp_s', 'uS', 'raw', 'stress', 'zscore'], 50, 8, cols=4, t0=12.0)
    rep3 = SessionSyncAudit(sd3, out_root=out).run()
    check('late GSR degraded', rep3['modalities']['gsr']['verdict'] == 'degraded')
    check('late session overall WARN', rep3['overall'] == 'WARN')

    # ── 4. Bad audio anchor (missing t_start) → out_of_sync ──
    sd4 = os.path.join(root, 'data', 'raw', 'session_badaudio')
    build_good(sd4)
    with open(os.path.join(sd4, 'audio', 'audio_meta.json'), 'w') as f:
        json.dump({'samplerate': 16000, 'channels': 1, 'n_frames': 16000 * 20}, f)
    rep4 = SessionSyncAudit(sd4, out_root=out).run()
    check('audio no-anchor out_of_sync',
          rep4['modalities']['audio']['verdict'] == 'out_of_sync')

    # ── 5. CSI without anchor (only csi_log.csv) → out_of_sync ──
    sd5 = os.path.join(root, 'data', 'raw', 'session_nocsianchor')
    build_good(sd5)
    os.remove(os.path.join(sd5, 'csi', 'csi_timestamped.csv'))
    _w(os.path.join(sd5, 'csi', 'csi_log.csv'), None or ['raw'],
       [['11548786,1,-60,128,' + ','.join(['30'] * 128)]])
    rep5 = SessionSyncAudit(sd5, out_root=out).run()
    check('csi unanchored out_of_sync',
          rep5['modalities']['wifi_csi']['verdict'] == 'out_of_sync')

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
