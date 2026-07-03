"""
Session Runner — drives a task protocol against a running recorder
==================================================================
The runner starts recording (via RecorderBridge), executes an ordered list of
Task objects, and stops. Every task boundary / cue / posture / response is
emitted as a marker on the recorder's master clock, so the offline synchronizer
can align "what the subject was doing" with the physiology.

Separation of concerns:
    - Tasks contain the protocol logic and emit markers via a RunContext.
    - RunContext is the tasks' only interface to the outside world: marking,
      interruptible waiting, participant prompts, response persistence, and UI
      events. It is fully injectable, so the whole protocol runs in tests with a
      fake recorder, an instant sleep, and canned responses — no hardware.

This module owns Tier-C protocols (breathing, body-scan, sit-stand, consent,
questionnaires); Tier-A/B stimulus presentation is layered on later.
"""

import json
import os
import threading
import time
from typing import Callable, List, Optional


class SessionControls:
    """Operator controls shared between the console and the runner: abort, pause/
    resume (holds the task timeline; recording keeps running), and skip (end the
    current task early). All are thread-safe Events."""

    def __init__(self, stop_event: Optional[threading.Event] = None):
        self.stop = stop_event or threading.Event()   # abort the whole session
        self.pause = threading.Event()                 # hold the task timeline
        self.skip = threading.Event()                  # end the current task

    def abort(self):
        self.stop.set()

    def request_pause(self):
        self.pause.set()

    def resume(self):
        self.pause.clear()

    def request_skip(self):
        self.skip.set()

    def is_paused(self) -> bool:
        return self.pause.is_set()


class RunContext:
    """The only interface a Task uses to reach the recorder / participant / UI."""

    def __init__(self, bridge, session_dir: Optional[str] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 stop_event: Optional[threading.Event] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 responder: Optional[Callable[..., object]] = None,
                 controls: Optional[SessionControls] = None):
        self.bridge = bridge
        self.session_dir = session_dir
        self._sleep = sleep
        self.controls = controls or SessionControls(stop_event)
        self.stop_event = self.controls.stop   # back-compat alias
        self._on_event = on_event
        self._responder = responder
        self._resp_path = (os.path.join(session_dir, 'responses.jsonl')
                           if session_dir else None)

    # ── control ──
    def aborted(self) -> bool:
        return self.controls.stop.is_set()

    def wait(self, seconds: float) -> bool:
        """
        Interruptible wait in <=0.1 s steps. Returns True if it completed (or was
        skipped), False if aborted partway. Honors pause (holds without consuming
        the remaining time) and skip (ends this wait early). Uses the injected
        sleep so tests run instantly.
        """
        remaining = float(seconds)
        while remaining > 0:
            if self.aborted():
                return False
            if self.controls.skip.is_set():
                return True                    # skip: end this wait; runner clears it
            if self.controls.pause.is_set():
                self._sleep(0.1)               # hold, don't consume remaining
                continue
            step = 0.1 if remaining > 0.1 else remaining
            self._sleep(step)
            remaining -= step
        return not self.aborted()

    # ── marking / UI / participant ──
    def mark(self, *a, **k):
        return self.bridge.mark(*a, **k)

    def emit(self, kind: str, **info):
        if self._on_event:
            self._on_event(kind, info)

    def ask(self, prompt: str, **kw):
        if self._responder is None:
            return None
        return self._responder(prompt, **kw)

    def record_response(self, task: str, data: dict):
        """Persist a participant response as one JSON line in the session dir."""
        rec = {'task': task, 'data': data, 't_wall': time.time()}
        if self._resp_path:
            with open(self._resp_path, 'a') as f:
                f.write(json.dumps(rec) + '\n')
        return rec


class SessionRunner:
    def __init__(self, bridge, tasks: List, subject: str,
                 sleep: Callable[[float], None] = time.sleep,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 responder: Optional[Callable[..., object]] = None,
                 stop_event: Optional[threading.Event] = None,
                 duration_margin_s: int = 15, audio: bool = False,
                 controls: Optional[SessionControls] = None):
        self.bridge = bridge
        self.tasks = tasks
        self.subject = subject
        self.sleep = sleep
        self.on_event = on_event
        self.responder = responder
        # One controls object drives abort/pause/skip; `stop_event` stays supported.
        self.controls = controls or SessionControls(stop_event)
        self.stop_event = self.controls.stop   # back-compat alias
        self.margin = duration_margin_s
        self.audio = audio

    def total_duration(self) -> int:
        planned = sum(getattr(t, 'planned_duration_s', 0.0) for t in self.tasks)
        return int(planned + self.margin)

    def abort(self):
        self.controls.abort()

    def _emit(self, kind: str, **info):
        if self.on_event:
            self.on_event(kind, info)

    def run(self) -> dict:
        info = self.bridge.start(self.subject, self.total_duration(),
                                 audio=self.audio)
        if not info.get('ok'):
            self._emit('recorder_refused', info=info)
            return {'ok': False, 'reason': 'recorder_refused',
                    'session_id': info.get('session_id')}

        ctx = RunContext(
            self.bridge,
            session_dir=getattr(self.bridge.recorder, 'session_dir', None),
            sleep=self.sleep, controls=self.controls,
            on_event=self.on_event, responder=self.responder,
        )
        completed, aborted, skipped = [], False, []
        for task in self.tasks:
            if self.controls.stop.is_set():
                aborted = True
                break
            self._emit('task_start', task=task.name)
            self.bridge.mark(f'task_start:{task.name}')
            try:
                task.run(ctx)
            except Exception as e:  # a bad task must not abort the whole session
                self.bridge.mark('task_error', task=task.name, error=str(e)[:200])
                self._emit('task_error', task=task.name, error=str(e))
            if self.controls.skip.is_set():   # operator skipped this task
                self.controls.skip.clear()
                self.bridge.mark(f'skip:{task.name}')
                self._emit('task_skipped', task=task.name)
                skipped.append(task.name)
            self._emit('task_end', task=task.name)
            completed.append(task.name)

        if self.controls.stop.is_set():
            aborted = True
            self.bridge.abort()
        res = self.bridge.stop()
        return {'ok': True, 'aborted': aborted,
                'session_id': res.get('session_id') or info.get('session_id'),
                'completed': completed, 'skipped': skipped}
