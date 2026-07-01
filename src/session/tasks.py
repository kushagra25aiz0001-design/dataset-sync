"""
Tier-C Tasks — coarse-timing / content-is-the-signal
====================================================
These tasks need boundary/cue accuracy on the master clock, not sub-frame onset
precision, so they run on the marker foundation alone (no flash-sync gate).

Each Task exposes:
    name                 : str
    planned_duration_s   : float   (best-effort, for the recorder auto-stop)
    run(ctx)             : drive the task, emitting markers via ctx.bridge

Tasks never touch hardware or the clock directly — everything goes through the
injected RunContext, so the whole protocol is testable without a recorder.
"""

from typing import List, Optional, Tuple


class Task:
    name = 'task'
    planned_duration_s = 0.0

    def run(self, ctx):
        raise NotImplementedError


class BreathingTask(Task):
    """
    Paced (box) breathing. The paced pattern is itself the ground-truth
    respiration signal for contactless (WiFi CSI / thermal) validation, so each
    phase boundary is logged as a cue on the master clock — that timing is the
    whole point, not just the block bounds.
    """
    name = 'breathing'

    def __init__(self, cycles: int = 10, pattern: Tuple[float, float, float, float] = (4, 4, 4, 4),
                 block_id: str = 'breathing', variant: str = 'v1'):
        self.cycles = cycles
        self.pattern = pattern           # inhale, hold_in, exhale, hold_out (s)
        self.block_id = block_id
        self.variant = variant
        self.planned_duration_s = cycles * sum(pattern)

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, pattern=list(self.pattern),
                               cycles=self.cycles, variant=self.variant)
        phases = [('inhale', self.pattern[0]), ('hold_in', self.pattern[1]),
                  ('exhale', self.pattern[2]), ('hold_out', self.pattern[3])]
        for c in range(self.cycles):
            for phase, dur in phases:
                if ctx.aborted():
                    ctx.bridge.block_end(self.block_id, aborted=True)
                    return
                ctx.bridge.cue(phase, cycle=c, planned_dur_s=dur)
                ctx.emit('breathing', phase=phase, dur=dur, cycle=c,
                         total_cycles=self.cycles)
                ctx.wait(dur)
        ctx.bridge.block_end(self.block_id)


class BodyScanTask(Task):
    """Passive eyes-closed low-arousal baseline (also the thermal warm-up window)."""
    name = 'body_scan'

    def __init__(self, duration_s: float = 240, block_id: str = 'body_scan'):
        self.duration_s = duration_s
        self.block_id = block_id
        self.planned_duration_s = duration_s

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, duration_s=self.duration_s)
        ctx.emit('instruction', text='Close your eyes and relax. Scan your body '
                                     'slowly from head to toe.')
        ctx.wait(self.duration_s)
        ctx.bridge.block_end(self.block_id)


class SitStandTask(Task):
    """
    Orthostatic sit↔stand. The BP / cardiovascular response must be tied to the
    exact posture transition, so each transition emits a posture marker; a
    bp_read cue prompts the operator to trigger/annotate a BP reading per posture.
    """
    name = 'sit_stand'

    def __init__(self, transitions: int = 4, hold_s: float = 60,
                 start: str = 'sit', block_id: str = 'sit_stand', bp: bool = True):
        self.transitions = transitions
        self.hold_s = hold_s
        self.start = start
        self.block_id = block_id
        self.bp = bp
        self.planned_duration_s = (transitions + 1) * hold_s

    def _enter(self, ctx, state):
        ctx.bridge.posture(state)
        ctx.emit('posture', state=state)
        if self.bp:
            ctx.bridge.cue('bp_read', posture=state)

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, transitions=self.transitions,
                               hold_s=self.hold_s)
        state = self.start
        self._enter(ctx, state)
        if not ctx.wait(self.hold_s):
            ctx.bridge.block_end(self.block_id, aborted=True)
            return
        for _ in range(self.transitions):
            if ctx.aborted():
                break
            state = 'stand' if state == 'sit' else 'sit'
            self._enter(ctx, state)
            if not ctx.wait(self.hold_s):
                break
        ctx.bridge.block_end(self.block_id)


class ConsentTask(Task):
    """Records consent; aborts the session if the participant declines."""
    name = 'consent'
    planned_duration_s = 0.0

    def __init__(self, prompt: str = 'Do you consent to participate in this '
                                     'recording session?'):
        self.prompt = prompt

    def run(self, ctx):
        agreed = bool(ctx.ask(self.prompt, kind='consent'))
        ctx.record_response('consent', {'agreed': agreed})
        ctx.bridge.mark('consent', agreed=agreed)
        if not agreed:
            ctx.emit('abort', reason='consent_declined')
            ctx.stop_event.set()


class QuestionnaireTask(Task):
    """
    A simple item-by-item questionnaire (trait/state labels — loose timing).
    Items: [{'key','prompt','choices'?}]. Answers persisted to responses.jsonl;
    a questionnaire_done marker is logged on the master clock.
    """
    name = 'questionnaire'
    planned_duration_s = 0.0

    def __init__(self, qname: str, items: List[dict], block_id: Optional[str] = None):
        self.qname = qname
        self.items = items
        self.block_id = block_id or f'q_{qname}'
        self.name = f'questionnaire:{qname}'

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, kind='questionnaire', name=self.qname)
        answers = {}
        for it in self.items:
            if ctx.aborted():
                break
            answers[it['key']] = ctx.ask(it['prompt'], key=it['key'],
                                         choices=it.get('choices'))
        ctx.record_response(self.qname, answers)
        ctx.bridge.mark(f'questionnaire_done:{self.qname}', n_items=len(self.items))
        ctx.bridge.block_end(self.block_id)
