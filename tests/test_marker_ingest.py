"""Marker ingest server tests: a separate task app POSTs events over real HTTP and
they land on the master clock in markers.csv, with device→master offset recorded.
Also checks the RecorderBridge reuses a recorder-owned marker log (no double log)."""
import csv, json, os, sys, tempfile, time, urllib.request, urllib.error

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.recorder.marker_ingest import MarkerIngestServer
from src.recorder.sync_markers import SyncMarkerLog
from src.recorder.recorder_bridge import RecorderBridge


def post(port, path, obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data,
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(port, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=3) as r:
        return r.status, json.loads(r.read())


def check(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        check.failed += 1
check.failed = 0


class FakeDaemon:
    """Recorder that owns its marker log, like the real headless daemon."""
    def __init__(self, root):
        self.session_dir = root
        self.rec_start = time.monotonic()
        self.markers = SyncMarkerLog(root, self.rec_start)
        self.captures_audio = False
        self.audio = None
        self._started = self._stopped = False

    def start_recording(self, subject, duration):
        self._started = True
        return 'session_fake'

    def stop_recording(self):
        self._stopped = True
        self.markers.session_end()
        self.markers.close()

    def mark(self, label, source='task', t_device_s=None, **payload):
        if self.markers is None:
            return None
        return self.markers.mark(label, source=source, t_device_s=t_device_s, **payload)

    def master_time(self):
        return time.monotonic() - self.rec_start


def main():
    root = tempfile.mkdtemp(prefix='ingest_')
    sd = os.path.join(root, 'sess'); os.makedirs(sd)
    daemon = FakeDaemon(sd)

    srv = MarkerIngestServer(daemon.mark, port=0,   # port 0 → OS picks a free port
                             health_fn=lambda: {'recording': True},
                             master_clock_fn=daemon.master_time).start()
    port = srv.port
    try:
        # ── external app posts an event with its own clock reading ──
        t_dev = 123.456
        code, resp = post(port, '/mark',
                          {'label': 'stim_onset:clip1', 'source': 'ipad',
                           't_device_s': t_dev, 'trial': 4})
        check('POST /mark 200', code == 200 and resp['ok'])
        check('server returned master time', isinstance(resp['t_master_s'], (int, float)))
        check('offset = master - device recorded',
              abs(resp['offset_s'] - (resp['t_master_s'] - t_dev)) < 1e-6)

        # ── a plain event with no device clock ──
        code, resp2 = post(port, '/mark', {'label': 'block_end:clip1'})
        check('POST /mark (no device clock) 200', code == 200 and resp2['ok'])

        # ── GET /time and /health ──
        code, t = get(port, '/time')
        check('GET /time returns master clock', code == 200 and t['t_master_s'] >= 0)
        code, h = get(port, '/health')
        check('GET /health ok', code == 200 and h.get('recording') is True)

        # ── bad request handling ──
        code, _ = post(port, '/mark', {'no_label': 1})
        check('missing label → 400', code == 400)

        # ── markers.csv actually contains the posted events on the master clock ──
        daemon.markers.close()   # flush
        rows = list(csv.DictReader(open(os.path.join(sd, 'markers.csv'))))
        labels = [r['label'] for r in rows]
        check('stim_onset written', 'stim_onset:clip1' in labels)
        r = next(x for x in rows if x['label'] == 'stim_onset:clip1')
        check('source + device time persisted',
              r['source'] == 'ipad' and r['t_device_s'] == '123.456')
        check('payload persisted', '"trial":4' in r['payload_json'])
    finally:
        srv.stop()

    # ── not-recording → 409 ──
    daemon2 = FakeDaemon(os.path.join(root, 's2'))
    daemon2.markers = None                    # simulate idle (no active session)
    srv2 = MarkerIngestServer(daemon2.mark, port=0).start()
    try:
        code, resp = post(srv2.port, '/mark', {'label': 'x'})
        check('idle recorder → 409 not_recording',
              code == 409 and resp['reason'] == 'not_recording')
    finally:
        srv2.stop()

    # ── bridge reuses the recorder-owned marker log (single writer) ──
    d3 = FakeDaemon(os.path.join(root, 's3'))
    owned = d3.markers
    bridge = RecorderBridge(d3)
    info = bridge.start('S1', 10)
    check('bridge reused daemon marker log', bridge.markers is owned)
    check('bridge did NOT take ownership', bridge._owns_markers is False)
    bridge.stim_onset('clipA')                # runner-side event
    d3.mark('response:clipA:1', source='task')  # app-side event, same log
    res = bridge.stop()   # delegates to recorder.stop_recording() → session_end+close
    check('bridge.stop left closing to the recorder',
          res['session_id'] == 'session_fake' and d3._stopped)
    rows = list(csv.DictReader(open(os.path.join(d3.session_dir, 'markers.csv'))))
    labels = [r['label'] for r in rows]
    check('both runner + app events in one markers.csv',
          'stim_onset:clipA' in labels and 'response:clipA:1' in labels)
    check('exactly one session_end', labels.count('session_end') == 1)

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
