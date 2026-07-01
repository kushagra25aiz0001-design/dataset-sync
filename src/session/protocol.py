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
    p = argparse.ArgumentParser(description='Run a Tier-C recording session.')
    p.add_argument('--subject', required=True)
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

    tasks = default_tier_c_protocol(short=args.short)
    runner = SessionRunner(bridge, tasks, subject=args.subject,
                           on_event=_console_event, responder=_console_responder)
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
