"""
Tier-B Tasks — ~100 ms boundary accuracy (audio / segment markers)
==================================================================
Tier-B needs the *boundaries* right, not sub-frame onsets: block/segment/response
markers on the master clock are sufficient. The one hard rule (brief §4/§5) is
that speech is captured by the recorder-owned audio stream on the master clock,
not the participant device — the runner starts that stream (audio=True); these
tasks only mark the speech windows.

Tasks:
    ArithmeticTask         serial subtraction (oral) — audio is the signal
    VerbalFluencyTask      category fluency (oral)
    PassageReadTask        read-aloud a passage
    PictureDescriptionTask describe a shown image
    SAMRatingTask          Self-Assessment Manikin (valence/arousal/dominance)
    TLXRatingTask          NASA-TLX workload (6 subscales)
"""

from typing import List, Optional, Sequence

from src.session.tasks import Task


class ArithmeticTask(Task):
    """Oral serial subtraction (a graded mental-stress task). Scored offline from
    the audio; here we mark the block + the speech window."""
    name = 'arithmetic'

    def __init__(self, start: int = 1000, step: int = 7, duration_s: float = 120,
                 block_id: str = 'arithmetic'):
        self.start = start
        self.step = step
        self.duration_s = duration_s
        self.block_id = block_id
        self.planned_duration_s = duration_s

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, kind='serial_subtraction',
                               start=self.start, step=self.step)
        ctx.emit('instruction', text=f'Starting at {self.start}, subtract {self.step} '
                                     f'repeatedly, out loud, as fast and accurately '
                                     f'as you can.')
        ctx.bridge.cue('speak_now')
        ctx.wait(self.duration_s)
        ctx.bridge.cue('stop')
        ctx.bridge.block_end(self.block_id)


class VerbalFluencyTask(Task):
    """Category fluency: name as many <category> as possible within the window."""
    name = 'verbal_fluency'

    def __init__(self, category: str = 'animals', duration_s: float = 60,
                 block_id: Optional[str] = None):
        self.category = category
        self.duration_s = duration_s
        self.block_id = block_id or f'fluency_{category}'
        self.planned_duration_s = duration_s
        self.name = f'verbal_fluency:{category}'

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, kind='verbal_fluency',
                               category=self.category)
        ctx.emit('instruction', text=f'Name as many {self.category} as you can.')
        ctx.bridge.cue('start')
        ctx.wait(self.duration_s)
        ctx.bridge.cue('stop')
        ctx.bridge.block_end(self.block_id)


class PassageReadTask(Task):
    """Read-aloud. Ends on operator confirmation (default) or after max_s."""
    name = 'passage_read'

    def __init__(self, passage_id: str, text: Optional[str] = None,
                 max_s: float = 120, wait_for_done: bool = True,
                 block_id: Optional[str] = None):
        self.passage_id = passage_id
        self.text = text
        self.max_s = max_s
        self.wait_for_done = wait_for_done
        self.block_id = block_id or f'read_{passage_id}'
        self.planned_duration_s = max_s
        self.name = f'passage_read:{passage_id}'

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, kind='passage_read',
                               passage_id=self.passage_id)
        ctx.emit('present_text', passage_id=self.passage_id, text=self.text)
        ctx.bridge.cue('read_start')
        if self.wait_for_done:
            ctx.ask('Press Enter when the participant finishes reading.',
                    kind='await_done', block=self.block_id)
        else:
            ctx.wait(self.max_s)
        ctx.bridge.cue('read_stop')
        ctx.bridge.block_end(self.block_id)


class PictureDescriptionTask(Task):
    """Describe a shown image for a fixed window."""
    name = 'picture_description'

    def __init__(self, picture_id: str, duration_s: float = 90,
                 block_id: Optional[str] = None):
        self.picture_id = picture_id
        self.duration_s = duration_s
        self.block_id = block_id or f'picture_{picture_id}'
        self.planned_duration_s = duration_s
        self.name = f'picture_description:{picture_id}'

    def run(self, ctx):
        ctx.bridge.block_start(self.block_id, kind='picture_description',
                               picture_id=self.picture_id)
        ctx.bridge.stim_onset(self.picture_id)
        ctx.emit('show_image', picture_id=self.picture_id)
        ctx.bridge.cue('describe')
        ctx.wait(self.duration_s)
        ctx.bridge.stim_offset(self.picture_id)
        ctx.bridge.block_end(self.block_id)


class SAMRatingTask(Task):
    """Self-Assessment Manikin — valence/arousal/dominance (1-9). The rating
    marker carries the values on the master clock (brief §5 acceptance)."""
    name = 'sam_rating'

    def __init__(self, stim_id: str,
                 scales: Sequence[str] = ('valence', 'arousal', 'dominance')):
        self.stim_id = stim_id
        self.scales = tuple(scales)
        self.name = f'sam_rating:{stim_id}'

    def run(self, ctx):
        vals = {}
        for dim in self.scales:
            if ctx.aborted():
                break
            vals[dim] = ctx.ask(f'Rate {dim} (1-9)', scale='SAM', dim=dim,
                                stim_id=self.stim_id, choices=list(range(1, 10)))
        ctx.record_response(f'SAM:{self.stim_id}', vals)
        ctx.bridge.rating('SAM', self.stim_id, **vals)


TLX_SUBSCALES = ['mental', 'physical', 'temporal', 'performance',
                 'effort', 'frustration']


class TLXRatingTask(Task):
    """NASA-TLX workload (0-100 per subscale) about a preceding block."""
    name = 'tlx_rating'

    def __init__(self, block_ref: str, subscales: Sequence[str] = TLX_SUBSCALES):
        self.block_ref = block_ref
        self.subscales = tuple(subscales)
        self.name = f'tlx_rating:{block_ref}'

    def run(self, ctx):
        vals = {}
        for dim in self.subscales:
            if ctx.aborted():
                break
            vals[dim] = ctx.ask(f'NASA-TLX {dim} (0-100)', scale='TLX', dim=dim,
                                block_ref=self.block_ref)
        ctx.record_response(f'TLX:{self.block_ref}', vals)
        ctx.bridge.rating('TLX', self.block_ref, **vals)
