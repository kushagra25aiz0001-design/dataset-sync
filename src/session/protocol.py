"""
Protocol builder + hardware CLI entry point for the Tier-C session runner
=========================================================================
Assembles a default Tier-C protocol and, from the command line, wires it to the
authoritative recorder (headless daemon, embedded in-process via RecorderBridge)
with a console UI and console prompts.

    python -m src.session.protocol --subject S01

The recorder embedding uses a lazy import (OpenCV / pyserial), so importing this
module for its `default_tier_c_protocol()` builder needs no hardware.
"""

import argparse
from typing import List

from src.session.tasks import (
    Task, ConsentTask, QuestionnaireTask, BreathingTask, BodyScanTask,
    SitStandTask,
)
from src.session.tier_b import (
    ArithmeticTask, VerbalFluencyTask, PictureDescriptionTask,
    SAMRatingTask, TLXRatingTask,
)


# ─── Default Tier-C protocol ─────────────────────────────────────────────────

PANAS_SHORT = [
    {'key': 'calm', 'prompt': 'Right now, how calm do you feel? (1-5)',
     'choices': [1, 2, 3, 4, 5]},
    {'key': 'tense', 'prompt': 'Right now, how tense do you feel? (1-5)',
     'choices': [1, 2, 3, 4, 5]},
    {'key': 'alert', 'prompt': 'Right now, how alert do you feel? (1-5)',
     'choices': [1, 2, 3, 4, 5]},
]


def default_tier_c_protocol(short: bool = False) -> List[Task]:
    """
    A runnable Tier-C session: consent → baseline questionnaire → body-scan
    baseline → paced breathing → sit/stand → closing questionnaire.
    `short=True` shrinks durations for a quick smoke test.
    """
    if short:
        return [
            ConsentTask(),
            QuestionnaireTask('pre_state', PANAS_SHORT),
            BodyScanTask(duration_s=6),
            BreathingTask(cycles=2, pattern=(1, 1, 1, 1)),
            SitStandTask(transitions=2, hold_s=3),
            QuestionnaireTask('post_state', PANAS_SHORT),
        ]
    return [
        ConsentTask(),
        QuestionnaireTask('pre_state', PANAS_SHORT),
        BodyScanTask(duration_s=240),
        BreathingTask(cycles=10, pattern=(4, 4, 4, 4), variant='v1'),
        SitStandTask(transitions=4, hold_s=60),
        QuestionnaireTask('post_state', PANAS_SHORT),
    ]


def default_tier_b_protocol(short: bool = False) -> List[Task]:
    """
    A Tier-B (speech / boundary-timed) session: mental arithmetic + workload
    rating, verbal fluency, and picture description + SAM rating. Needs the
    recorder-owned audio stream (run with audio=True).
    """
    if short:
        return [
            ConsentTask(),
            ArithmeticTask(start=100, step=7, duration_s=4),
            TLXRatingTask('arithmetic'),
            VerbalFluencyTask('animals', duration_s=3),
            PictureDescriptionTask('pic_demo', duration_s=3),
            SAMRatingTask('pic_demo'),
        ]
    return [
        ConsentTask(),
        ArithmeticTask(start=1000, step=7, duration_s=120),
        TLXRatingTask('arithmetic'),
        VerbalFluencyTask('animals', duration_s=60),
        VerbalFluencyTask('fruits', duration_s=60),
        PictureDescriptionTask('cookie_theft', duration_s=90),
        SAMRatingTask('cookie_theft'),
    ]


def build_protocol(tier: str, short: bool = False) -> List[Task]:
    """tier: 'c' | 'b' | 'cb' (Tier-C then Tier-B)."""
    if tier == 'c':
        return default_tier_c_protocol(short)
    if tier == 'b':
        return default_tier_b_protocol(short)
    return default_tier_c_protocol(short) + default_tier_b_protocol(short)[1:]


# ─── Console UI helpers (hardware run) ───────────────────────────────────────

def _console_event(kind: str, info: dict):
    if kind == 'task_start':
        print(f'\n▶ {info.get("task")}')
    elif kind == 'breathing':
        print(f'   {info["phase"]:>8}  ({info["dur"]}s)  cycle {info["cycle"]+1}/{info["total_cycles"]}')
    elif kind == 'posture':
        print(f'   posture → {info["state"].upper()}')
    elif kind == 'instruction':
        print(f'   {info["text"]}')
    elif kind == 'abort':
        print(f'   ⚠ abort: {info.get("reason")}')


def _console_responder(prompt: str, **kw):
    kind = kw.get('kind')
    if kind == 'consent':
        return input(f'   {prompt} [y/N] ').strip().lower().startswith('y')
    ans = input(f'   {prompt} ').strip()
    choices = kw.get('choices')
    if choices:
        try:
            return type(choices[0])(ans)
        except (ValueError, IndexError):
            return ans
    return ans


def main():
    p = argparse.ArgumentParser(description='Run a recording session protocol.')
    p.add_argument('--subject', required=True)
    p.add_argument('--tier', choices=['c', 'b', 'cb'], default='c',
                   help="c=Tier-C, b=Tier-B (speech), cb=both")
    p.add_argument('--audio', action='store_true',
                   help='Capture recorder-owned audio (required for Tier-B speech)')
    p.add_argument('--short', action='store_true', help='Quick smoke-test durations')
    p.add_argument('--warmup', type=float, default=5.0,
                   help='Seconds to let sensors connect before recording')
    # recorder passthroughs
    p.add_argument('--camera-source', default='auto')
    p.add_argument('--oxi-port', default='auto')
    p.add_argument('--csi-port', default='/dev/ttyUSB1')
    p.add_argument('--emg-port', default='auto')
    p.add_argument('--gsr-port', default='auto')
    args = p.parse_args()

    from src.recorder.recorder_bridge import RecorderBridge
    from src.session.runner import SessionRunner

    bridge, daemon = RecorderBridge.embed_headless_daemon(
        warmup_s=args.warmup,
        camera_source=args.camera_source, oxi_port=args.oxi_port,
        csi_port=args.csi_port, emg_port=args.emg_port, gsr_port=args.gsr_port,
    )

    # Operator health check before starting
    health = bridge.get_health()
    print('\nSensor health:')
    for name, s in health['sensors'].items():
        flag = '✅' if s.get('ok') else '❌'
        print(f'  {flag} {name:>9}: {s.get("state")}')

    tasks = build_protocol(args.tier, short=args.short)
    audio = args.audio or args.tier in ('b', 'cb')
    runner = SessionRunner(bridge, tasks, subject=args.subject,
                           on_event=_console_event, responder=_console_responder,
                           audio=audio)
    try:
        result = runner.run()
    except KeyboardInterrupt:
        print('\n⚠ interrupted — aborting session')
        runner.abort()
        result = {'ok': False, 'aborted': True}
    finally:
        daemon.stop_monitoring()

    print(f'\nSession complete: {result}')


if __name__ == '__main__':
    main()
