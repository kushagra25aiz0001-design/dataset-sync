"""
Health Monitor — live per-sensor liveness for the operator console
==================================================================
The recorder's registry exposes each sensor's *state* and a monotonically-rising
*sample counter*. Neither tells the operator "is data actually flowing right now?"
This module turns a stream of health snapshots (from ``RecorderBridge.get_health``)
into a live board: per-sensor **rate (Hz)** and a **stall** flag (counter stopped
advancing), plus an overall verdict.

It is deliberately pure logic — you feed it snapshots + timestamps and it returns
a board dict; the console just renders it. That makes the liveness logic unit-
testable without any hardware, threads, or a terminal.
"""

from typing import Dict, Optional


class HealthMonitor:
    def __init__(self, min_hz: Optional[Dict[str, float]] = None,
                 stall_after_s: float = 3.0, ema: float = 0.5):
        """
        Args:
            min_hz: per-sensor minimum acceptable rate (below → 'degraded').
            stall_after_s: if a sensor's counter hasn't advanced for this long
                           (while it claims to be streaming), flag it 'stalled'.
            ema: smoothing factor for the rate estimate (0<ema<=1; 1 = no smoothing).
        """
        self.min_hz = min_hz or {}
        self.stall_after_s = stall_after_s
        self.ema = ema
        self._prev: Dict[str, tuple] = {}       # name -> (t, count)
        self._rate: Dict[str, float] = {}        # name -> smoothed Hz
        self._last_advance: Dict[str, float] = {}  # name -> t of last count increase

    def update(self, health: dict, now: float) -> dict:
        """Enrich one health snapshot with rate + stall. `health` is what
        RecorderBridge.get_health() returns. Returns the board dict."""
        board = {
            'recording': health.get('recording'),
            'session_id': health.get('session_id'),
            'sensors': {},
        }
        worst = 'ok'
        for name, s in health.get('sensors', {}).items():
            count = s.get('count')
            state = s.get('state')
            ok = s.get('ok')
            hz = 0.0
            stalled = False
            if count is not None:
                prev = self._prev.get(name)
                if prev is not None:
                    dt = now - prev[0]
                    if dt > 0:
                        inst = (count - prev[1]) / dt
                        prev_rate = self._rate.get(name, inst)
                        hz = self.ema * inst + (1 - self.ema) * prev_rate
                    if count > prev[1]:
                        self._last_advance[name] = now
                else:
                    self._last_advance[name] = now
                self._prev[name] = (now, count)
                self._rate[name] = hz
                # stall: streaming-ish but the counter has been flat too long
                streaming = ok or (isinstance(state, str) and 'stream' in state.lower())
                if streaming and (now - self._last_advance.get(name, now)) >= self.stall_after_s:
                    stalled = True

            floor = self.min_hz.get(name)
            below = floor is not None and hz < floor and not stalled
            status = ('stalled' if stalled else
                      'low' if below else
                      'ok' if (ok or hz > 0) else 'offline')
            if stalled and worst != 'stalled':
                worst = 'stalled'
            elif below and worst == 'ok':
                worst = 'degraded'
            board['sensors'][name] = {
                'state': state, 'ok': ok, 'count': count,
                'hz': round(hz, 1), 'stalled': stalled, 'status': status,
            }
        board['overall'] = worst
        return board


def render_board(board: dict, elapsed_s: Optional[float] = None) -> str:
    """Render a board dict (from HealthMonitor.update) as a fixed-width table."""
    icon = {'ok': '✅', 'low': '⚠️ ', 'stalled': '⛔', 'offline': '⬛'}
    rec = '🔴 REC' if board.get('recording') else '⬜ idle'
    head = f"{rec}  {board.get('session_id') or '-'}"
    if elapsed_s is not None:
        head += f"  ⏱ {int(elapsed_s)//60:02d}:{int(elapsed_s)%60:02d}"
    lines = [head, '  ' + '-' * 40,
             f"  {'sensor':<10}{'Hz':>7}  {'count':>8}  status"]
    for name, s in board.get('sensors', {}).items():
        lines.append(f"  {icon.get(s['status'], '? ')} {name:<8}{s['hz']:>7.1f}  "
                     f"{(s['count'] if s['count'] is not None else 0):>8}  {s['status']}")
    lines.append('  ' + '-' * 40)
    verdict = {'ok': '✅ all live', 'degraded': '⚠️  degraded',
               'stalled': '⛔ STALL'}.get(board.get('overall'), board.get('overall'))
    lines.append(f"  overall: {verdict}")
    return '\n'.join(lines)
