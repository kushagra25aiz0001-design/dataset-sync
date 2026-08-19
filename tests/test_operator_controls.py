"""Operator console dispatch + runner pause/skip/abort wait() behavior."""
import os, sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.session.runner import RunContext, SessionControls
from src.session.operator_console import OperatorConsole


class FakeBridge:
    def __init__(self):
        self.marks = []
        self._health = {'recording': True, 'session_id': 'sess_x',
                        'sensors': {'oxi': {'count': 10, 'ok': True, 'state': 'streaming'},
                                    'cam': {'count': 5, 'ok': True, 'state': 'streaming'}}}

    def get_health(self):
        return self._health

    def mark(self, label, **k):
        self.marks.append(label)
        return {'t_master_s': 1.0, 'label': label}

    def pause(self, **k): return self.mark('pause')
    def resume(self, **k): return self.mark('resume')
    def abort(self, **k): return self.mark('abort')


def check(name, cond, extra=''):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}  {extra}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    # ── console dispatch ──
    bridge = FakeBridge()
    controls = SessionControls()
    con = OperatorConsole(bridge, controls)

    txt = con.render_once(now=con._start_t + 65)   # 65 s elapsed
    check('render shows sensors + verdict', 'oxi' in txt and 'cam' in txt
          and 'overall' in txt and '01:05' in txt)

    check("'p' pauses", con._dispatch('p') == 'pause'
          and controls.is_paused() and 'pause' in bridge.marks)
    check("'r' resumes", con._dispatch('r') == 'resume'
          and not controls.is_paused() and 'resume' in bridge.marks)
    check("'s' skips", con._dispatch('s') == 'skip' and controls.skip.is_set())
    check("'a' aborts", con._dispatch('a') == 'abort'
          and controls.stop.is_set() and 'abort' in bridge.marks)
    check('unknown key ignored', con._dispatch('z') is None)

    # ── runner wait() honors controls ──
    # abort → wait returns False
    c2 = SessionControls()
    ctx = RunContext(bridge, controls=c2, sleep=lambda s: None)
    c2.abort()
    check('abort → wait returns False', ctx.wait(5.0) is False)

    # skip → wait returns True immediately (no time consumed)
    c3 = SessionControls()
    calls = []
    ctx3 = RunContext(bridge, controls=c3, sleep=lambda s: calls.append(s))
    c3.request_skip()
    check('skip → wait returns True with no sleeps', ctx3.wait(100.0) is True and calls == [])

    # pause → holds until resumed, then completes
    c4 = SessionControls()
    c4.request_pause()
    held = {'n': 0}
    def paused_sleep(s):
        held['n'] += 1
        if held['n'] == 3:        # operator resumes after a few poll cycles
            c4.resume()
    ctx4 = RunContext(bridge, controls=c4, sleep=paused_sleep)
    done = ctx4.wait(0.05)
    check('pause holds then completes', done is True and held['n'] >= 3,
          f"sleeps={held['n']}")

    print(f"\n{'ALL PASSED' if check.failed == 0 else str(check.failed)+' FAILED'}")
    return check.failed


if __name__ == '__main__':
    raise SystemExit(1 if main() else 0)
