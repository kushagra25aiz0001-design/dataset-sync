"""
PyTorch Dataset / DataLoader
============================
Serves multi-modal rPPG windows produced by builder.py and grouped by
splitter.py, for a chosen split (train/val/test).

Each item is a dict of tensors:
    inputs : {modality: (win_len, n_channels)}   e.g. 'cam', 'csi', 'emg', 'gsr'
    pleth  : (win_len,)        per-sample oximeter waveform target
    hr     : ()                per-window heart rate
    spo2   : ()                per-window SpO2
    meta   : {session, index}

Design notes
------------
- The window **index** (which (session, window) pairs to serve) is built purely
  from JSON — ``datasets/splits.json`` plus each session's
  ``windows_manifest.json`` (which carries the per-window ``valid`` flags). This
  core is dependency-free and unit-tested; only ``__getitem__`` touches numpy.
- ``windows.npz`` is opened lazily with ``mmap_mode='r'`` and cached per session,
  so a window is sliced from disk without loading the whole array.
- Sessions missing any requested input modality are skipped (counted in
  ``skipped_sessions``), so every served item has a consistent shape — the
  default collate then batches them without a custom collate_fn.

numpy is required at run time; torch is required to construct the Dataset/loader
(both imported lazily so this module imports and its index core tests without
either installed).

Usage
-----
    from src.dataset.loader import make_dataloader
    dl = make_dataloader('datasets/splits.json', split='train',
                         inputs=('cam', 'csi'), target='pleth', batch_size=16)
    for batch in dl:
        x_cam = batch['inputs']['cam']   # (B, win_len, 3)
        y = batch['pleth']               # (B, win_len)
"""

import json
import os
from typing import Dict, List, Optional, Sequence, Tuple


# ─── Index core (pure standard library, unit-tested) ─────────────────────────

def _load_manifest(processed_root: str, session_id: str) -> Optional[dict]:
    path = os.path.join(processed_root, session_id, 'windows_manifest.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return None


def build_window_index(splits_json: str, split: str,
                       inputs: Sequence[str],
                       processed_root: Optional[str] = None,
                       valid_only: bool = True
                       ) -> Tuple[List[Tuple[str, int]], dict]:
    """
    Build the list of (session_id, window_idx) to serve for `split`.

    A session is included only if its manifest exposes every requested input
    modality; otherwise it is skipped. When `valid_only`, only windows whose
    manifest `valid` flag is 1 are served. Returns (index, info) where info
    reports skipped sessions and missing-input reasons. Pure standard library.
    """
    with open(splits_json) as f:
        splits = json.load(f)
    if split not in splits.get('splits', {}):
        raise ValueError(f'unknown split {split!r}; have {list(splits["splits"])}')
    proot = processed_root or splits.get('processed_root', 'data/processed')

    index: List[Tuple[str, int]] = []
    skipped: List[dict] = []
    for sid in splits['splits'][split]['sessions']:
        man = _load_manifest(proot, sid)
        if man is None:
            skipped.append({'session': sid, 'reason': 'no manifest'})
            continue
        present = set(man.get('inputs', {}).keys())
        missing = [m for m in inputs if m not in present]
        if missing:
            skipped.append({'session': sid, 'reason': f'missing inputs {missing}'})
            continue
        flags = man.get('valid') or [1] * man.get('n_windows', 0)
        for wi, v in enumerate(flags):
            if (not valid_only) or v:
                index.append((sid, wi))
    info = {'split': split, 'processed_root': proot,
            'n_items': len(index), 'n_sessions_used':
            len({s for s, _ in index}), 'skipped': skipped}
    return index, info


# ─── PyTorch Dataset (lazy torch/numpy via a cached factory) ─────────────────

_DATASET_CLASS = None  # built once, on first use, to subclass torch's Dataset


def _dataset_class():
    """
    Define (once) and return the Dataset subclass. torch is imported here, not at
    module load, so this file imports — and its pure-Python index core is testable
    — without torch installed. Cleaner and safer than mutating __bases__.
    """
    global _DATASET_CLASS
    if _DATASET_CLASS is not None:
        return _DATASET_CLASS
    import torch
    import torch.utils.data as tud
    import numpy as np

    class _RPPGWindowDataset(tud.Dataset):
        def __init__(self, splits_json, split='train', inputs=('cam', 'csi'),
                     target='pleth', processed_root=None,
                     valid_only=True, normalize=True):
            self.inputs = tuple(inputs)
            self.target = target
            self.normalize = normalize
            self.index, self.info = build_window_index(
                splits_json, split, self.inputs, processed_root, valid_only)
            self.processed_root = self.info['processed_root']
            self._cache = {}                      # session_id → npz handle

        def __len__(self):
            return len(self.index)

        def _npz(self, session_id):
            if session_id not in self._cache:
                path = os.path.join(self.processed_root, session_id, 'windows.npz')
                self._cache[session_id] = np.load(path, mmap_mode='r')
            return self._cache[session_id]

        def __getitem__(self, i):
            sid, wi = self.index[i]
            npz = self._npz(sid)
            inp = {}
            for m in self.inputs:
                arr = np.asarray(npz[m][wi], dtype='float32')   # (win_len, ch)
                if self.normalize:
                    mu = arr.mean(axis=0, keepdims=True)
                    sd = arr.std(axis=0, keepdims=True)
                    arr = (arr - mu) / (sd + 1e-6)
                inp[m] = torch.from_numpy(arr)
            item = {'inputs': inp,
                    'hr': torch.tensor(float(npz['hr'][wi])),
                    'spo2': torch.tensor(float(npz['spo2'][wi])),
                    'meta': {'session': sid, 'index': int(wi)}}
            if self.target == 'pleth' and 'pleth' in npz:
                y = np.asarray(npz['pleth'][wi], dtype='float32')
                if self.normalize:
                    y = (y - y.mean()) / (y.std() + 1e-6)
                item['pleth'] = torch.from_numpy(y)
            elif self.target in ('hr', 'spo2'):
                item['target'] = item[self.target]
            return item

    _DATASET_CLASS = _RPPGWindowDataset
    return _DATASET_CLASS


def RPPGWindowDataset(splits_json: str, split: str = 'train',
                      inputs: Sequence[str] = ('cam', 'csi'),
                      target: str = 'pleth',
                      processed_root: Optional[str] = None,
                      valid_only: bool = True, normalize: bool = True):
    """Construct the torch Dataset for a split (factory over a lazily-built class)."""
    return _dataset_class()(splits_json, split=split, inputs=inputs, target=target,
                            processed_root=processed_root, valid_only=valid_only,
                            normalize=normalize)


def _collate(batch):
    """Collate dicts with a nested per-modality 'inputs' dict + 'meta' passthrough."""
    import torch
    out = {}
    inputs = {m: torch.stack([b['inputs'][m] for b in batch]) for m in batch[0]['inputs']}
    out['inputs'] = inputs
    for k in batch[0]:
        if k in ('inputs', 'meta'):
            continue
        out[k] = torch.stack([b[k] for b in batch])
    out['meta'] = [b['meta'] for b in batch]
    return out


def make_dataloader(splits_json: str, split: str = 'train',
                    inputs: Sequence[str] = ('cam', 'csi'),
                    target: str = 'pleth', batch_size: int = 16,
                    shuffle: Optional[bool] = None, num_workers: int = 0,
                    processed_root: Optional[str] = None,
                    valid_only: bool = True, normalize: bool = True):
    """Construct a torch DataLoader for the given split."""
    import torch.utils.data as tud
    ds = RPPGWindowDataset(splits_json, split=split, inputs=inputs, target=target,
                           processed_root=processed_root, valid_only=valid_only,
                           normalize=normalize)
    if shuffle is None:
        shuffle = (split == 'train')
    return tud.DataLoader(ds, batch_size=batch_size, shuffle=shuffle,
                          num_workers=num_workers, collate_fn=_collate)


# ─── CLI: summarize what a split would serve (no torch needed) ───────────────

def main():
    import argparse
    p = argparse.ArgumentParser(
        description='Summarize the windows a split would serve (index only).')
    p.add_argument('--splits', type=str, default='datasets/splits.json')
    p.add_argument('--split', type=str, default='train',
                   choices=['train', 'val', 'test'])
    p.add_argument('--inputs', type=str, nargs='+', default=['cam', 'csi'])
    p.add_argument('--include-invalid', action='store_true')
    args = p.parse_args()

    index, info = build_window_index(args.splits, args.split, args.inputs,
                                     valid_only=not args.include_invalid)
    print(f'  split={info["split"]}: {info["n_items"]} windows from '
          f'{info["n_sessions_used"]} session(s), inputs={"+".join(args.inputs)}')
    for sk in info['skipped']:
        print(f'    skipped {sk["session"]}: {sk["reason"]}')


if __name__ == '__main__':
    main()
