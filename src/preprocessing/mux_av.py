"""
Mux audio + video for review
============================
The recorder keeps audio and video in SEPARATE files by design — ``camera/
recording.avi`` (video only; OpenCV can't write an audio track) and
``audio/audio.wav`` — both timestamped on the master clock. That is ideal for
multi-modal sync, but it means the ``.avi`` has no sound, which surprises people.

This tool muxes them into one playable file with the audio shifted by the true
master-clock offset, so you can *watch + listen* and confirm the audio recorded
(e.g. the DJI-mic-via-audio-jack track). It does not touch the originals.

    python -m src.preprocessing.mux_av --session data/raw/session_YYYYMMDD_HHMMSS

Requires ffmpeg on PATH. The offset math + timestamp parsing are pure-stdlib
(unit-tested); ffmpeg does the actual muxing.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
from typing import Optional


def av_offset(video_first_ts: float, audio_t_start: float) -> float:
    """Seconds to shift the audio relative to the video in the muxed file.
    Both are master-clock times: the video's first real frame and the audio's
    first sample. Positive → audio started later → delay it; negative → audio
    started earlier → trim its head."""
    return audio_t_start - video_first_ts


def first_real_frame_ts(timestamps_csv: str) -> Optional[float]:
    """Master-clock timestamp of the first genuinely-captured video frame
    (is_real==1 if present), or None."""
    if not os.path.exists(timestamps_csv):
        return None
    with open(timestamps_csv, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or 'timestamp_s' not in header:
            return None
        ti = header.index('timestamp_s')
        ri = header.index('is_real') if 'is_real' in header else None
        for row in reader:
            if len(row) <= ti:
                continue
            if ri is not None and (len(row) <= ri or row[ri].strip() != '1'):
                continue
            try:
                return float(row[ti])
            except ValueError:
                continue
    return None


def _find_video(session_dir: str) -> Optional[str]:
    cam = os.path.join(session_dir, 'camera')
    if not os.path.isdir(cam):
        return None
    for name in sorted(os.listdir(cam)):
        if name.startswith('recording.') and os.path.getsize(os.path.join(cam, name)) > 0:
            return os.path.join(cam, name)
    return None


def mux_session(session_dir: str, out_path: Optional[str] = None) -> dict:
    """Mux the session's video + audio into one file. Returns a result dict."""
    if shutil.which('ffmpeg') is None:
        return {'ok': False, 'reason': 'ffmpeg not found on PATH (apt install ffmpeg)'}
    video = _find_video(session_dir)
    if not video:
        return {'ok': False, 'reason': 'no camera/recording.* video found'}
    wav = os.path.join(session_dir, 'audio', 'audio.wav')
    meta_path = os.path.join(session_dir, 'audio', 'audio_meta.json')
    if not os.path.exists(wav) or not os.path.exists(meta_path):
        return {'ok': False, 'reason': 'no audio/audio.wav (audio was not recorded — '
                                       'check the audio device selection)'}
    try:
        meta = json.load(open(meta_path))
        audio_t0 = float(meta.get('t_start_master_s') or 0.0)
    except (ValueError, OSError) as e:
        return {'ok': False, 'reason': f'unreadable audio_meta.json: {e}'}

    vid_first = first_real_frame_ts(
        os.path.join(session_dir, 'camera', 'timestamps.csv')) or 0.0
    offset = av_offset(vid_first, audio_t0)
    out_path = out_path or os.path.join(session_dir, 'camera', 'recording_with_audio.mp4')

    # -itsoffset shifts the AUDIO input's timestamps by `offset` (delay if > 0).
    cmd = ['ffmpeg', '-y', '-i', video]
    if offset >= 0:
        cmd += ['-itsoffset', f'{offset:.3f}', '-i', wav]
    else:
        # audio started before video → trim its head instead of a negative offset
        cmd += ['-ss', f'{-offset:.3f}', '-i', wav]
    cmd += ['-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac',
            '-shortest', out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return {'ok': False, 'reason': 'ffmpeg failed', 'stderr': proc.stderr[-400:]}
    return {'ok': True, 'output': out_path, 'audio_offset_s': round(offset, 3),
            'audio_device': meta.get('device_name')}


def main():
    p = argparse.ArgumentParser(description='Mux a session\'s audio + video for review.')
    p.add_argument('--session', required=True, help='data/raw/session_YYYYMMDD_HHMMSS')
    p.add_argument('--out', default=None)
    args = p.parse_args()
    res = mux_session(args.session, args.out)
    if res['ok']:
        print(f"✅ wrote {res['output']}  (audio offset {res['audio_offset_s']}s, "
              f"device: {res['audio_device']})")
    else:
        print(f"❌ {res['reason']}")
        if res.get('stderr'):
            print(res['stderr'])
        raise SystemExit(1)


if __name__ == '__main__':
    main()
