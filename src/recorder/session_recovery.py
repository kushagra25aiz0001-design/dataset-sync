"""
Session Recovery — salvage a recording whose recorder crashed mid-session
=========================================================================
If the daemon is killed (crash, power loss, Ctrl-C at the wrong moment) after data
is on disk but before ``stop_recording()`` runs, the session is left "open":
``metadata.json`` has no ``duration_actual``/``stats`` and ``markers.csv`` has no
``session_end``. The raw CSVs are fine (every handler appends + line-buffers), so
**no samples are lost** — the session just needs finalizing.

This module detects such sessions and reconstructs the missing bookkeeping from the
data that IS on disk: per-modality row counts, the actual duration (max timestamp
across modalities), and a closing ``session_end`` marker. The result is a normal,
audit-able session flagged ``recovered: true``.

Pure standard library.

    python -m src.recorder.session_recovery --scan
    python -m src.recorder.session_recovery --finalize data/raw/session_YYYYMMDD_HHMMSS
"""

import argparse
import csv
import glob
import json
import os
import time
from typing import List, Optional, Tuple

# modality → (relative file, master-clock timestamp column)
_RECOVER_FILES = {
    'cam': ('camera/timestamps.csv', 'timestamp_s'),
    'oxi': ('oximeter/oximeter_log.csv', 'timestamp_s'),
    'csi': ('csi/csi_timestamped.csv', 'pc_timestamp_s'),
    'emg': ('emg/emg_log.csv', 'timestamp_s'),
    'gsr': ('gsr/gsr_log.csv', 'timestamp_s'),
    'thermal': ('thermal/timestamps.csv', 'timestamp_s'),
    'ecg': ('ecg/ecg_log.csv', 'timestamp_s'),
}


def _count_and_maxts(path: str, ts_col: str) -> Tuple[int, Optional[float]]:
    """(data-row count, max timestamp) for one modality CSV; (0, None) if absent."""
    if not os.path.exists(path):
        return 0, None
    n = 0
    max_t = None
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or ts_col not in header:
            return 0, None
        ti = header.index(ts_col)
        for row in reader:
            if len(row) <= ti:
                continue
            n += 1
            try:
                t = float(row[ti])
            except ValueError:
                continue
            if max_t is None or t > max_t:
                max_t = t
    return n, max_t


def looks_interrupted(session_dir: str) -> bool:
    """True if the session has data but was never finalized (crash)."""
    meta_path = os.path.join(session_dir, 'metadata.json')
    if not os.path.exists(meta_path):
        return False
    try:
        meta = json.load(open(meta_path))
    except (ValueError, OSError):
        return False
    if 'duration_actual' in meta and 'stats' in meta:
        return False                     # already finalized
    # only call it interrupted if there is actually some data on disk
    for rel, ts in _RECOVER_FILES.values():
        n, _ = _count_and_maxts(os.path.join(session_dir, rel), ts)
        if n > 0:
            return True
    return False


def scan_interrupted(raw_root: str = 'data/raw') -> List[str]:
    """List session dirs under raw_root that look interrupted."""
    out = []
    for meta in sorted(glob.glob(os.path.join(raw_root, 'session_*', 'metadata.json'))):
        sd = os.path.dirname(meta)
        if looks_interrupted(sd):
            out.append(sd)
    return out


def _append_session_end(session_dir: str, t_master: float) -> bool:
    """Append a session_end marker if markers.csv exists and lacks one."""
    path = os.path.join(session_dir, 'markers.csv')
    if not os.path.exists(path):
        return False
    has_end = False
    with open(path, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header and 'label' in header:
            li = header.index('label')
            for row in reader:
                if len(row) > li and row[li] == 'session_end':
                    has_end = True
                    break
    if has_end:
        return False
    with open(path, 'a', newline='') as f:
        # columns: t_master_s,label,source,t_device_s,offset_s,payload_json
        csv.writer(f).writerow([round(t_master, 6), 'session_end', 'recovery',
                                '', '', '{"recovered":true}'])
    return True


def finalize_interrupted(session_dir: str) -> dict:
    """Reconstruct stats + duration + session_end for a crashed session and mark it
    recovered. Returns the updated metadata. Idempotent-safe: re-running just
    recomputes from the (unchanged) data."""
    meta_path = os.path.join(session_dir, 'metadata.json')
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path))
        except (ValueError, OSError):
            meta = {}

    stats = {}
    duration = 0.0
    for key, (rel, ts) in _RECOVER_FILES.items():
        n, max_t = _count_and_maxts(os.path.join(session_dir, rel), ts)
        stats[key] = n
        if max_t is not None:
            duration = max(duration, max_t)

    # audio duration (from its meta) also bounds the session length
    ameta = os.path.join(session_dir, 'audio', 'audio_meta.json')
    if os.path.exists(ameta):
        try:
            am = json.load(open(ameta))
            t0 = am.get('t_start_master_s') or 0.0
            duration = max(duration, float(t0) + float(am.get('duration_s') or 0.0))
        except (ValueError, OSError):
            pass

    meta['stats'] = stats
    meta['duration_actual'] = round(duration, 2)
    meta['recovered'] = True
    meta['recovered_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    ended = _append_session_end(session_dir, duration)
    meta['recovered_session_end'] = ended

    os.makedirs(session_dir, exist_ok=True)
    with open(meta_path, 'w') as f:
        json.dump(meta, f, indent=2)
    return meta


def main():
    p = argparse.ArgumentParser(description='Detect + finalize crashed sessions.')
    p.add_argument('--scan', action='store_true', help='List interrupted sessions')
    p.add_argument('--finalize', type=str, default=None, help='Finalize one session dir')
    p.add_argument('--all', action='store_true', help='Finalize every interrupted session')
    p.add_argument('--raw-root', type=str, default='data/raw')
    args = p.parse_args()

    if args.finalize:
        meta = finalize_interrupted(args.finalize)
        print(f"✓ recovered {os.path.basename(args.finalize)}: "
              f"dur={meta['duration_actual']}s stats={meta['stats']}")
        return
    interrupted = scan_interrupted(args.raw_root)
    if not interrupted:
        print('No interrupted sessions found.')
        return
    print(f'Interrupted sessions ({len(interrupted)}):')
    for sd in interrupted:
        print(f'  • {os.path.basename(sd)}')
        if args.all:
            meta = finalize_interrupted(sd)
            print(f'      → recovered: dur={meta["duration_actual"]}s stats={meta["stats"]}')


if __name__ == '__main__':
    main()
