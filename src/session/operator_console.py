"""
Operator Console — live control surface for a recording session
===============================================================
A terminal console the operator watches while a session runs. It polls the
recorder's health, renders a live board (via HealthMonitor), and turns single
keystrokes into control actions + master-clock markers:

    p  pause      → hold the task timeline (recording keeps running), mark 'pause'
    r  resume     → mark 'resume'
    s  skip       → end the current task early, mark 'skip:<task>'
    a  abort      → stop the whole session, mark 'abort'
    q  quit       → leave the console (does NOT stop recording)

The control/marker logic (`_dispatch`) and the rendering (`render_once`) are pure
and unit-tested; only the raw-terminal keyboard reader + the poll thread are the
untestable glue (they need a TTY).
"""

import sys
import threading
import time
from typing import Optional

from src.session.health_monitor import HealthMonitor, render_board
from src.session.runner import SessionControls

_HINT = '  [p]ause  [r]esume  [s]kip  [a]bort  [q]uit'


class OperatorConsole:
    def __init__(self, bridge, controls: Optional[SessionControls] = None,
                 poll_s: float = 0.5, min_hz: Optional[dict] = None):
        self.bridge = bridge
        self.controls = controls or SessionControls()
        self.poll_s = poll_s
        self.monitor = HealthMonitor(min_hz=min_hz)
        self._stop = threading.Event()
        self._start_t = time.monotonic()
        self._last = ''

    # ── pure, testable surfaces ───────────────────────────────────────────────

    def render_once(self, now: Optional[float] = None) -> str:
        """Poll health once and return the rendered board (no I/O side effects)."""
        now = time.monotonic() if now is None else now
        health = self.bridge.get_health()
        board = self.monitor.update(health, now)
        self._last = render_board(board, elapsed_s=now - self._start_t) + '\n' + _HINT
        return self._last

    def _dispatch(self, key: str) -> Optional[str]:
        """Map a keystroke to a control action + marker. Returns the action name,
        or None if the key isn't a command."""
        key = (key or '').lower()
        if key == 'p':
            self.controls.request_pause()
            self.bridge.pause()
            return 'pause'
        if key == 'r':
            self.controls.resume()
            self.bridge.resume()
            return 'resume'
        if key == 's':
            self.controls.request_skip()
            return 'skip'          # the runner emits skip:<task> when the task ends
        if key == 'a':
            self.controls.abort()
            self.bridge.abort()
            return 'abort'
        if key == 'q':
            self._stop.set()
            return 'quit'
        return None

    # ── terminal glue (needs a TTY; not unit-tested) ──────────────────────────

    def start(self):
        """Start the poll/render thread and the keyboard thread (if a TTY)."""
        self._stop.clear()
        threading.Thread(target=self._poll_loop, daemon=True,
                         name='op-console-poll').start()
        if sys.stdin and sys.stdin.isatty():
            threading.Thread(target=self._key_loop, daemon=True,
                             name='op-console-keys').start()

    def stop(self):
        self._stop.set()

    def _poll_loop(self):
        while not self._stop.is_set():
            try:
                sys.stdout.write('\033[2J\033[H')      # clear + home
                sys.stdout.write(self.render_once())
                sys.stdout.write('\n')
                sys.stdout.flush()
            except Exception:
                pass
            time.sleep(self.poll_s)

    def _key_loop(self):
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._stop.is_set():
                ch = sys.stdin.read(1)
                if ch:
                    self._dispatch(ch)
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
