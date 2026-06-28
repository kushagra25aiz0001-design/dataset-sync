"""
Dataset Splitter
================
Assigns windowed sessions to train / val / test splits **subject-disjointly** —
no subject ever appears in more than one split. This is essential for
physiological ML: if the same person's windows land in both train and test, the
model can memorize their identity/baseline and report inflated accuracy.

Inputs
------
- ``data/processed/<session_id>/windows_manifest.json`` (from builder.py) for the
  valid-window count per session.
- ``data/raw/<session_id>/metadata.json`` for each session's ``subject``. If the
  metadata is missing, the session id is used as its own subject (degrades to
  session-disjoint) and a warning is recorded.

Method
------
Whole subjects are assigned greedily (after a seeded shuffle) to whichever split
is furthest below its target share of total valid windows. Output is written to
``datasets/splits.json``.

Pure standard library — deterministic given ``--seed``.

Usage
-----
    python -m src.dataset.splitter --ratios 0.7 0.15 0.15 --seed 42
"""

import argparse
import json
import os
import random
from typing import Dict, List, Optional, Tuple


def _read_subject(raw_root: str, session_id: str) -> Optional[str]:
    """Read the subject id from a session's raw metadata.json, or None."""
    meta = os.path.join(raw_root, session_id, 'metadata.json')
    if not os.path.exists(meta):
        return None
    try:
        with open(meta) as f:
            subj = json.load(f).get('subject')
        return str(subj) if subj else None
    except (ValueError, OSError):
        return None


def _discover_sessions(processed_root: str) -> List[Tuple[str, int]]:
    """Return [(session_id, n_valid_windows), ...] for sessions that were built."""
    out = []
    if not os.path.isdir(processed_root):
        return out
    for d in sorted(os.listdir(processed_root)):
        man = os.path.join(processed_root, d, 'windows_manifest.json')
        if not os.path.exists(man):
            continue
        try:
            with open(man) as f:
                n_valid = int(json.load(f).get('n_valid', 0))
        except (ValueError, OSError):
            n_valid = 0
        out.append((d, n_valid))
    return out


def split_subjects(subject_counts: Dict[str, int],
                   ratios: Tuple[float, float, float],
                   seed: int = 42) -> Dict[str, str]:
    """
    Greedily assign whole subjects to ('train','val','test') to approximate the
    target window-count ratios. Returns {subject: split}. Deterministic.
    """
    names = ['train', 'val', 'test']
    total = sum(subject_counts.values()) or 1
    targets = {s: r * total for s, r in zip(names, ratios)}
    got = {s: 0 for s in names}
    assignment: Dict[str, str] = {}

    # Largest subjects first (after a seeded shuffle to break ties reproducibly)
    subjects = list(subject_counts.items())
    rng = random.Random(seed)
    rng.shuffle(subjects)
    subjects.sort(key=lambda kv: kv[1], reverse=True)

    for subj, cnt in subjects:
        # pick the split with the largest remaining deficit (target - got),
        # but never starve val/test: a split already at/over target is skipped
        # unless all are over (then fall back to the most-deficient).
        deficits = {s: targets[s] - got[s] for s in names}
        under = [s for s in names if deficits[s] > 0] or names
        choice = max(under, key=lambda s: deficits[s])
        assignment[subj] = choice
        got[choice] += cnt
    return assignment


class DatasetSplitter:
    def __init__(self, processed_root: str = 'data/processed',
                 raw_root: str = 'data/raw',
                 ratios: Tuple[float, float, float] = (0.7, 0.15, 0.15),
                 seed: int = 42, out_dir: str = 'datasets'):
        s = sum(ratios)
        self.ratios = tuple(r / s for r in ratios)  # normalize
        self.processed_root = processed_root
        self.raw_root = raw_root
        self.seed = seed
        self.out_dir = out_dir

    def run(self) -> dict:
        warnings: List[str] = []
        sessions = _discover_sessions(self.processed_root)
        if not sessions:
            return {'error': f'no built sessions under {self.processed_root}',
                    'warnings': warnings}

        # subject → its sessions and total valid windows
        subj_of: Dict[str, str] = {}
        subj_sessions: Dict[str, List[str]] = {}
        subj_counts: Dict[str, int] = {}
        for sid, n_valid in sessions:
            subj = _read_subject(self.raw_root, sid)
            if subj is None:
                subj = sid
                warnings.append(f'{sid}: no subject in metadata — treated as its '
                                f'own subject (session-disjoint)')
            subj_of[sid] = subj
            subj_sessions.setdefault(subj, []).append(sid)
            subj_counts[subj] = subj_counts.get(subj, 0) + n_valid

        if len(subj_counts) < 3:
            warnings.append(f'only {len(subj_counts)} subject(s) — cannot fill all '
                            f'three splits subject-disjointly; some will be empty')

        assignment = split_subjects(subj_counts, self.ratios, self.seed)

        splits = {s: {'subjects': [], 'sessions': [], 'n_windows': 0}
                  for s in ['train', 'val', 'test']}
        by_session: Dict[str, str] = {}
        for subj, split in assignment.items():
            splits[split]['subjects'].append(subj)
            for sid in subj_sessions[subj]:
                splits[split]['sessions'].append(sid)
                by_session[sid] = split
            splits[split]['n_windows'] += subj_counts[subj]

        total = sum(subj_counts.values()) or 1
        result = {
            'seed': self.seed,
            'ratios': {'train': self.ratios[0], 'val': self.ratios[1],
                       'test': self.ratios[2]},
            'processed_root': self.processed_root,
            'n_subjects': len(subj_counts),
            'n_sessions': len(sessions),
            'total_windows': total,
            'splits': {
                s: {**splits[s],
                    'actual_ratio': round(splits[s]['n_windows'] / total, 3)}
                for s in splits
            },
            'by_session': by_session,
            'warnings': warnings,
        }
        os.makedirs(self.out_dir, exist_ok=True)
        with open(os.path.join(self.out_dir, 'splits.json'), 'w') as f:
            json.dump(result, f, indent=2)
        return result


def _build_parser():
    p = argparse.ArgumentParser(
        description='Subject-disjoint train/val/test split of built sessions.')
    p.add_argument('--processed-root', type=str, default='data/processed')
    p.add_argument('--raw-root', type=str, default='data/raw')
    p.add_argument('--ratios', type=float, nargs=3, default=[0.7, 0.15, 0.15],
                   metavar=('TRAIN', 'VAL', 'TEST'))
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--out', type=str, default='datasets')
    return p


def main():
    args = _build_parser().parse_args()
    res = DatasetSplitter(processed_root=args.processed_root, raw_root=args.raw_root,
                          ratios=tuple(args.ratios), seed=args.seed,
                          out_dir=args.out).run()
    if 'error' in res:
        print(f'  ✗ {res["error"]}')
        return
    print(f'  ✓ {res["n_sessions"]} sessions / {res["n_subjects"]} subjects, '
          f'{res["total_windows"]} valid windows → datasets/splits.json')
    for s in ['train', 'val', 'test']:
        d = res['splits'][s]
        print(f'    {s:>5}: {len(d["subjects"])} subj, {len(d["sessions"])} sess, '
              f'{d["n_windows"]} win ({d["actual_ratio"]*100:.0f}%)')
    if res['warnings']:
        print(f'    ({len(res["warnings"])} warning(s))')


if __name__ == '__main__':
    main()
