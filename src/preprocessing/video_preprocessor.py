"""
Video Preprocessor
==================
Extracts a per-frame skin-ROI RGB signal from a recorded camera video — the raw
material for remote photoplethysmography (rPPG).

Pipeline
--------
1. Read ``camera/timestamps.csv`` to learn which AVI frames are *real* captures
   (``is_real == 1``); FRC-inserted duplicate frames are skipped (they carry no
   new pixels and a synthetic timestamp). Older sessions without the column are
   treated as all-real.
2. Decode ``camera/recording.*`` frame by frame (duplicates are grabbed but not
   decoded, to save CPU).
3. Detect the face and extract forehead + cheek ROIs, then average the pixels to
   one mean (R, G, B) per frame. Face detector backend:
       - MediaPipe Face Mesh if installed (precise, stable face box), else
       - OpenCV Haar cascade (bundled with opencv-python) as a fallback.
   Both feed the same fractional ROI geometry, so output is consistent.
4. Optionally band-pass the green channel (0.7–4.0 Hz ≈ 42–240 bpm) to a crude
   pulse estimate (requires scipy; skipped with a note if absent).
5. Write ``rgb_signal.csv`` (+ ``rgb_report.json``) under
   ``data/processed/<session_id>/``. A downstream step joins it to the
   synchronizer's aligned grid via ``cam_frame_idx``.

Heavy dependencies (opencv-python, optionally mediapipe/scipy) are imported
lazily so this module can be imported, ``--help``-ed, and unit-tested (its ROI
geometry and frame-table parsing are pure standard library) without them.

Usage
-----
    python -m src.preprocessing.video_preprocessor --session data/raw/session_YYYYMMDD_HHMMSS
    python -m src.preprocessing.video_preprocessor --all --backend haar
"""

import argparse
import csv
import glob
import json
import math
import os
from typing import Dict, List, Optional, Tuple


# ─── Pure-stdlib helpers (unit-testable without cv2/numpy) ───────────────────

def _roi_rects_from_bbox(x: int, y: int, w: int, h: int,
                         frame_w: int, frame_h: int) -> Dict[str, Tuple[int, int, int, int]]:
    """
    Given a face bounding box (x, y, w, h), return integer pixel rectangles
    (rx, ry, rw, rh) for the forehead and both cheeks as fractions of the box.
    Rectangles are clamped to the frame. This fractional geometry is shared by
    both face-detector backends so their output is comparable.

    Forehead : upper-central band (skin, low motion, strong pulse signal).
    Cheeks   : mid-height left/right patches (well-perfused, avoids eyes/mouth).
    """
    def clamp_rect(rx, ry, rw, rh):
        rx = max(0, min(int(rx), frame_w - 1))
        ry = max(0, min(int(ry), frame_h - 1))
        rw = max(1, min(int(rw), frame_w - rx))
        rh = max(1, min(int(rh), frame_h - ry))
        return (rx, ry, rw, rh)

    forehead = clamp_rect(x + 0.25 * w, y + 0.07 * h, 0.50 * w, 0.18 * h)
    l_cheek = clamp_rect(x + 0.13 * w, y + 0.55 * h, 0.22 * w, 0.20 * h)
    r_cheek = clamp_rect(x + 0.65 * w, y + 0.55 * h, 0.22 * w, 0.20 * h)
    return {'forehead': forehead, 'left_cheek': l_cheek, 'right_cheek': r_cheek}


def _load_frame_table(timestamps_csv: str):
    """
    Header-driven read of camera/timestamps.csv. Returns
    (frame_idxs, timestamps, used_is_real) keeping only is_real==1 rows when the
    column exists (else all rows). Pure standard library.
    """
    if not os.path.exists(timestamps_csv):
        return None
    frame_idxs: List[int] = []
    times: List[float] = []
    with open(timestamps_csv, newline='', errors='replace') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return None
        idx = {name: i for i, name in enumerate(header)}
        if 'frame_idx' not in idx or 'timestamp_s' not in idx:
            return None
        fi, ti = idx['frame_idx'], idx['timestamp_s']
        ri = idx.get('is_real')
        used_is_real = ri is not None
        for row in reader:
            if len(row) <= max(fi, ti):
                continue
            if used_is_real and (len(row) <= ri or row[ri].strip() != '1'):
                continue
            try:
                frame_idxs.append(int(float(row[fi])))
                times.append(float(row[ti]))
            except ValueError:
                continue
    if not frame_idxs:
        return None
    return frame_idxs, times, used_is_real


def _find_video(cam_dir: str) -> Optional[str]:
    """Locate the recording video file in a camera/ directory."""
    for ext in ('.avi', '.mp4', '.mkv'):
        hits = glob.glob(os.path.join(cam_dir, f'recording{ext}'))
        if hits and os.path.getsize(hits[0]) > 0:
            return hits[0]
    hits = sorted(glob.glob(os.path.join(cam_dir, 'recording.*')))
    return hits[0] if hits else None


def _bandpass(signal, fs: float, lo: float = 0.7, hi: float = 4.0):
    """
    Detrend + band-pass a 1-D signal to the cardiac band. Uses scipy if present;
    returns (filtered_list, applied: bool). If scipy is unavailable or the signal
    is too short, returns the input unchanged with applied=False.
    """
    n = len(signal)
    if n < 30 or fs <= 0:
        return list(signal), False
    try:
        import numpy as np
        from scipy.signal import butter, filtfilt, detrend
    except ImportError:
        return list(signal), False
    nyq = 0.5 * fs
    lo_n, hi_n = lo / nyq, min(hi / nyq, 0.99)
    if not (0 < lo_n < hi_n < 1):
        return list(signal), False
    b, a = butter(3, [lo_n, hi_n], btype='band')
    x = detrend(np.asarray(signal, dtype=float))
    try:
        y = filtfilt(b, a, x)
    except ValueError:
        return list(signal), False
    return [float(v) for v in y], True


# ─── Face ROI extraction (needs cv2; optionally mediapipe) ───────────────────

class FaceROIExtractor:
    """
    Detects a face and returns the mean (R, G, B) over the forehead+cheek ROIs.
    Backend is chosen at construction: 'mediapipe', 'haar', or 'auto'.
    """

    def __init__(self, backend: str = 'auto'):
        import cv2  # hard requirement for any extraction
        self._cv2 = cv2
        self.backend = None
        self._mesh = None
        self._cascade = None

        if backend in ('auto', 'mediapipe'):
            try:
                import mediapipe as mp
                self._mp = mp
                self._mesh = mp.solutions.face_mesh.FaceMesh(
                    static_image_mode=False, max_num_faces=1,
                    refine_landmarks=False, min_detection_confidence=0.5,
                    min_tracking_confidence=0.5)
                self.backend = 'mediapipe'
            except ImportError:
                if backend == 'mediapipe':
                    raise
        if self.backend is None:
            path = os.path.join(cv2.data.haarcascades,
                                'haarcascade_frontalface_default.xml')
            self._cascade = cv2.CascadeClassifier(path)
            self.backend = 'haar'

    def _face_bbox(self, frame) -> Optional[Tuple[int, int, int, int]]:
        """Return (x, y, w, h) of the largest detected face, or None."""
        cv2 = self._cv2
        h, w = frame.shape[:2]
        if self.backend == 'mediapipe':
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = self._mesh.process(rgb)
            if not res.multi_face_landmarks:
                return None
            xs = [lm.x for lm in res.multi_face_landmarks[0].landmark]
            ys = [lm.y for lm in res.multi_face_landmarks[0].landmark]
            x0, x1 = min(xs) * w, max(xs) * w
            y0, y1 = min(ys) * h, max(ys) * h
            return (int(x0), int(y0), int(x1 - x0), int(y1 - y0))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self._cascade.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
        if len(faces) == 0:
            return None
        return tuple(int(v) for v in max(faces, key=lambda f: f[2] * f[3]))

    def process(self, frame) -> Tuple[bool, Tuple[float, float, float]]:
        """Return (face_found, (r_mean, g_mean, b_mean)) over the skin ROIs."""
        import numpy as np
        bbox = self._face_bbox(frame)
        if bbox is None:
            return False, (math.nan, math.nan, math.nan)
        h, w = frame.shape[:2]
        rects = _roi_rects_from_bbox(*bbox, frame_w=w, frame_h=h)
        patches = []
        for (rx, ry, rw, rh) in rects.values():
            patch = frame[ry:ry + rh, rx:rx + rw]
            if patch.size:
                patches.append(patch.reshape(-1, 3))
        if not patches:
            return False, (math.nan, math.nan, math.nan)
        px = np.concatenate(patches, axis=0).astype('float64')
        b, g, r = px[:, 0].mean(), px[:, 1].mean(), px[:, 2].mean()  # OpenCV is BGR
        return True, (float(r), float(g), float(b))

    def close(self):
        if self._mesh is not None:
            self._mesh.close()


# ─── Session driver ──────────────────────────────────────────────────────────

class VideoPreprocessor:
    def __init__(self, session_dir: str, out_root: str = 'data/processed',
                 backend: str = 'auto'):
        self.session_dir = os.path.abspath(session_dir)
        self.session_id = os.path.basename(self.session_dir.rstrip('/'))
        self.out_root = out_root
        self.backend = backend

    def run(self) -> dict:
        import cv2
        cam_dir = os.path.join(self.session_dir, 'camera')
        table = _load_frame_table(os.path.join(cam_dir, 'timestamps.csv'))
        if table is None:
            return {'session_id': self.session_id, 'error': 'no camera/timestamps.csv'}
        frame_idxs, times, used_is_real = table
        real_set = set(frame_idxs)
        ts_by_idx = dict(zip(frame_idxs, times))

        video = _find_video(cam_dir)
        if video is None:
            return {'session_id': self.session_id, 'error': 'no recording video found'}

        cap = cv2.VideoCapture(video)
        if not cap.isOpened():
            return {'session_id': self.session_id, 'error': f'cannot open {video}'}

        extractor = FaceROIExtractor(self.backend)
        out_idx: List[int] = []
        out_t: List[float] = []
        out_face: List[int] = []
        out_rgb: List[Tuple[float, float, float]] = []

        k = 0
        faces_found = 0
        try:
            while True:
                if not cap.grab():           # advance without decoding
                    break
                if k in real_set:
                    ok, frame = cap.retrieve()
                    if ok and frame is not None:
                        found, (r, g, b) = extractor.process(frame)
                        out_idx.append(k)
                        out_t.append(ts_by_idx.get(k, float('nan')))
                        out_face.append(1 if found else 0)
                        out_rgb.append((r, g, b))
                        faces_found += int(found)
                k += 1
        finally:
            cap.release()
            extractor.close()

        # Optional band-passed pulse estimate on the green channel
        fs = 0.0
        if len(out_t) > 1:
            span = out_t[-1] - out_t[0]
            fs = (len(out_t) - 1) / span if span > 0 else 0.0
        green = [g for (_, g, _) in out_rgb]
        pulse, filtered = _bandpass(green, fs)

        # Write outputs
        out_dir = os.path.join(self.out_root, self.session_id)
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, 'rgb_signal.csv')
        with open(csv_path, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['frame_idx', 'timestamp_s', 'face_detected',
                        'roi_r', 'roi_g', 'roi_b', 'pulse_green'])
            for i in range(len(out_idx)):
                r, g, b = out_rgb[i]
                w.writerow([out_idx[i], f'{out_t[i]:.4f}', out_face[i],
                            r, g, b, pulse[i] if filtered else ''])

        n = len(out_idx)
        report = {
            'session_id': self.session_id,
            'video': os.path.basename(video),
            'backend': extractor.backend,
            'frames_processed': n,
            'faces_detected': faces_found,
            'face_detection_rate': round(faces_found / n, 3) if n else 0.0,
            'used_is_real_filter': used_is_real,
            'effective_fps': round(fs, 2),
            'bandpass_applied': filtered,
            'output_csv': csv_path,
        }
        with open(os.path.join(out_dir, 'rgb_report.json'), 'w') as f:
            json.dump(report, f, indent=2)
        return report


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser():
    p = argparse.ArgumentParser(
        description='Extract per-frame face-ROI RGB (rPPG source) from a session video.')
    p.add_argument('--session', type=str, default=None)
    p.add_argument('--all', action='store_true')
    p.add_argument('--raw-root', type=str, default='data/raw')
    p.add_argument('--out', type=str, default='data/processed')
    p.add_argument('--backend', choices=['auto', 'mediapipe', 'haar'], default='auto')
    return p


def main():
    args = _build_parser().parse_args()
    if args.all:
        targets = [os.path.join(args.raw_root, d)
                   for d in sorted(os.listdir(args.raw_root))
                   if d.startswith('session_')
                   and os.path.isdir(os.path.join(args.raw_root, d))] \
            if os.path.isdir(args.raw_root) else []
    elif args.session:
        targets = [args.session]
    else:
        _build_parser().error('provide --session <dir> or --all')

    if not targets:
        print('No sessions found.')
        return

    for sd in targets:
        try:
            report = VideoPreprocessor(sd, out_root=args.out, backend=args.backend).run()
        except Exception as e:
            print(f'  ✗ {os.path.basename(sd)}: {e}')
            continue
        if 'error' in report:
            print(f'  ✗ {report["session_id"]}: {report["error"]}')
            continue
        print(f'  ✓ {report["session_id"]}: {report["frames_processed"]} frames, '
              f'face {report["face_detection_rate"]*100:.0f}% '
              f'[{report["backend"]}], bandpass={report["bandpass_applied"]}')


if __name__ == '__main__':
    main()
