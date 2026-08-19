"""End-to-end: run the Tier-C short protocol against a fake recorder, then audit
the produced session. Verifies BP markers, questionnaire, audio delegation, and
that the marker stream lands on the master clock."""
import csv, os, sys, tempfile, time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.session.protocol import build_protocol
from src.session.runner import SessionRunner
from src.recorder.recorder_bridge import RecorderBridge
from src.preprocessing.sync_audit import SessionSyncAudit


class FakeRecorder:
    """Minimal recorder satisfying the bridge contract (no hardware)."""
    def __init__(self, root, captures_audio=False):
        self.root = root
        self.rec_start = None
        self.session_dir = None
        self.registry = None
        self.captures_audio = captures_audio
        self.audio = None

    def start_recording(self, subject, duration):
        self.rec_start = time.monotonic()
        sid = 'session_fake'
        self.session_dir = os.path.join(self.root, 'data', 'raw', sid)
        for sub in ['camera', 'oximeter', 'csi', 'emg', 'gsr', 'thermal', 'audio']:
            os.makedirs(os.path.join(self.session_dir, sub), exist_ok=True)
        # Emit a couple of physiology rows so the audit has raw streams to align.
        for name, header in [
            ('oximeter/oximeter_log.csv',
             ['timestamp_s', 'spo2', 'heart_rate', 'signal_strength', 'pleth']),
            ('gsr/gsr_log.csv', ['timestamp_s', 'uS', 'raw', 'stress', 'zscore'])]:
            with open(os.path.join(self.session_dir, name), 'w', newline='') as f:
                w = csv.writer(f); w.writerow(header)
                for i in range(300):
                    w.writerow([f'{i/60.0:.4f}'] + [i % 90] * (len(header) - 1))
        return sid

    def stop_recording(self):
        pass


def markers(session_dir):
    out = []
    with open(os.path.join(session_dir, 'markers.csv'), newline='') as f:
        for row in csv.DictReader(f):
            out.append(row)
    return out


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    root = tempfile.mkdtemp(prefix='session_e2e_')
    rec = FakeRecorder(root, captures_audio=False)   # no daemon audio → bridge path
    bridge = RecorderBridge(rec)

    answers = iter([True,            # consent
                    3, 3, 3,         # pre_state PANAS
                    120, 80, 66,     # BP rest sys/dia/pulse
                    3, 3, 3])        # post_state PANAS
    def responder(prompt, **kw):
        if kw.get('kind') == 'consent':
            return next(answers)
        return next(answers, 3)

    tasks = build_protocol('c', short=True)
    runner = SessionRunner(bridge, tasks, subject='S99',
                           sleep=lambda s: None,    # instant
                           responder=responder, audio=True)
    result = runner.run()
    check('session ok', result['ok'] and not result['aborted'])

    sd = rec.session_dir
    ms = markers(sd)
    labels = [m['label'] for m in ms]

    check('session_start marked', 'session_start' in labels)
    check('BP reading marked', any(l == 'rating:BP:rest' for l in labels))
    bp = next(m for m in ms if m['label'] == 'rating:BP:rest')
    check('BP payload carries sys/dia', '"systolic":120' in bp['payload_json']
          and '"diastolic":80' in bp['payload_json'])
    check('consent marked', 'consent' in labels)
    check('breathing cues present', 'cue:inhale' in labels)
    check('posture markers present', any(l.startswith('posture:') for l in labels))
    check('questionnaire_done marked',
          any(l.startswith('questionnaire_done:') for l in labels))
    # No daemon audio → bridge started its own AudioRecorder, which degrades to
    # 'audio_unavailable' with no mic in the sandbox (must NOT crash the session).
    check('audio degraded gracefully (no mic)', 'audio_unavailable' in labels)
    # Master clock: all marker times monotonic and start near 0.
    ts = [float(m['t_master_s']) for m in ms]
    check('markers on master clock (start ~0, monotonic)',
          ts == sorted(ts) and 0 <= ts[0] < 2.0)

    # Now audit the produced session.
    rep = SessionSyncAudit(sd, out_root=os.path.join(root, 'processed')).run()
    check('audit: questionnaire in_sync',
          rep['modalities']['questionnaire']['verdict'] == 'in_sync')
    check('audit: bp in_sync', rep['modalities']['bp']['verdict'] == 'in_sync')

    # ── audio delegation: recorder that owns audio → bridge must NOT double-start ──
    rec2 = FakeRecorder(root + '2', captures_audio=True)
    rec2.audio = object()                 # pretend the daemon started audio
    b2 = RecorderBridge(rec2)
    info = b2.start('S98', 10, audio=True)
    check('delegation: bridge did not create its own AudioRecorder',
          b2.audio is None and info['ok'])
    l2 = [m['label'] for m in markers(rec2.session_dir)]
    check('delegation: audio_start attributed to recorder',
          'audio_start' in l2)
    b2.stop()

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
