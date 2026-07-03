"""
Modality Manifest — the single source of truth for all recorded modalities
==========================================================================
This project records a multi-modal physiological dataset for contactless
cardiovascular / affect sensing. There are 11 *conceptual* modalities. Some are
physically captured streams, some are **derived** from a captured stream (rPPG and
micro-expressions are computed from the facial video, not sensed separately), and
one is an **event** stream (the questionnaire lives in the marker log).

Every captured stream is placed on ONE master clock: ``time.monotonic()`` sampled
at ``rec_start`` (the recorder's ``t0``). Each per-sample stream writes
``timestamp_s = monotonic() - rec_start`` (starting near 0); CSI additionally
carries the ESP32 device tick and a PC-clock anchor; audio anchors its first
sample via ``t_start_master_s``; the questionnaire/markers use ``t_master_s``.
They therefore all share the same origin, which is exactly what makes them
alignable — and exactly what :mod:`src.preprocessing.sync_audit` verifies.

This module is the declarative registry the audit (and, going forward, the
recorder) reads. It is pure standard library and imports no hardware libs, so it
is safe to import anywhere.

Field reference (``Modality``):
    id            canonical key
    label         human-readable name
    category      'raw'  physically captured time-series/stream
                  'derived' computed offline from another modality
                  'event'   discrete markers on the master clock
    clock         how it lands on the master clock:
                  'per_sample_ts'   a timestamp_s column, one per sample
                  'device+pc_anchor' CSI: device tick + PC-clock anchor file
                  'anchor_meta'     one anchor time for a continuous stream (audio)
                  'markers'         rows in markers.csv (t_master_s)
                  'derived'         inherits the clock of `derived_from`
    tier          sync-accuracy tier required by the science:
                  'A' sub-frame / onset-accurate (rPPG, micro-expr, ECG, EMG, audio onsets)
                  'B' ~100 ms boundaries (thermal, CSI)
                  'C' seconds / content-is-the-signal (GSR, questionnaire, BP)
    ts_file       (per_sample_ts / device+pc_anchor) CSV holding the timestamps
    ts_col        name of the master-clock timestamp column in ts_file
    real_col      optional column that flags genuine samples (camera is_real)
    expected_hz   (min, nominal, max) sampling-rate band, or None
    derived_from  (derived) id of the source modality
    ground_truth  optional id of the reference signal used to validate it
    paths         representative output paths (for existence checks / docs)
    required      whether every session is expected to contain it
    notes         free-form
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Modality:
    id: str
    label: str
    category: str                 # 'raw' | 'derived' | 'event'
    clock: str                    # see module docstring
    tier: str                     # 'A' | 'B' | 'C'
    ts_file: Optional[str] = None
    ts_col: str = 'timestamp_s'
    real_col: Optional[str] = None
    expected_hz: Optional[Tuple[float, float, float]] = None
    derived_from: Optional[str] = None
    ground_truth: Optional[str] = None
    paths: Tuple[str, ...] = ()
    required: bool = False
    notes: str = ''


# ── The 11 conceptual modalities (+ the oximeter ground-truth instrument) ─────
#
# The user's 11: facial video, audio, text questionnaire, rPPG, micro-expressions,
# thermal camera, BP, WiFi, ECG, EMG, GSR. `oximeter` is listed explicitly because
# it is the *recorded* ground-truth PPG that rPPG is trained/validated against; it
# is a real captured stream the audit must check, so it earns a manifest entry.

MODALITIES: List[Modality] = [
    Modality(
        id='facial_video', label='Facial video (RGB)', category='raw',
        clock='per_sample_ts', tier='A',
        ts_file='camera/timestamps.csv', ts_col='timestamp_s', real_col='is_real',
        expected_hz=(24.0, 30.0, 60.0),
        paths=('camera/timestamps.csv', 'camera/recording.avi'),
        required=True,
        notes='Master RGB stream; rPPG and micro-expressions are derived from it. '
              'is_real==0 rows are FRC duplicates with synthetic timestamps.'),
    Modality(
        id='audio', label='Audio (mic, recorder-owned)', category='raw',
        clock='anchor_meta', tier='A',
        ts_file='audio/audio_meta.json', ts_col='t_start_master_s',
        expected_hz=(16000.0, 16000.0, 48000.0),
        paths=('audio/audio.wav', 'audio/audio_meta.json'),
        notes='Corresponds to the facial video. Anchored to the master clock at '
              'the first callback; NOT the participant-device mic.'),
    Modality(
        id='questionnaire', label='Text questionnaire / task events', category='event',
        clock='markers', tier='C',
        ts_file='markers.csv', ts_col='t_master_s',
        paths=('markers.csv', 'responses.jsonl'),
        notes='Ratings/responses and task boundaries recorded as markers on the '
              'master clock.'),
    Modality(
        id='rppg', label='rPPG (remote PPG)', category='derived',
        clock='derived', tier='A',
        derived_from='facial_video', ground_truth='oximeter',
        ts_file='processed:rgb_signal.csv', ts_col='timestamp_s',
        paths=('(processed) rgb_signal.csv',),
        notes='Face-ROI RGB extracted from facial_video; inherits its clock. '
              'Ground truth = oximeter pleth.'),
    Modality(
        id='micro_expression', label='Micro-expressions', category='derived',
        clock='derived', tier='A',
        derived_from='facial_video',
        paths=('(derived from facial_video)',),
        notes='Computed from the RGB facial video; needs high, steady frame rate. '
              'Inherits facial_video timing; frame-rate/gap quality is critical.'),
    Modality(
        id='thermal', label='Thermal camera', category='raw',
        clock='per_sample_ts', tier='B',
        ts_file='thermal/timestamps.csv', ts_col='timestamp_s', real_col='is_real',
        expected_hz=(8.0, 25.0, 60.0),
        paths=('thermal/timestamps.csv', 'thermal/frame_*.png'),
        notes='Live thermal frames grabbed ON the PC (UVC/V4L2 node OR a screen-'
              'shared window), so each frame is timestamped on the master clock at '
              'grab time — no camera-RTC offset. Works for the FLIR C5 via USB/screen '
              'streaming. Frames are lossless PNG, real only (is_real always 1). '
              'Caveat: a live feed is palette-COLORIZED, not absolute-°C radiometric '
              '(that needs offline FLIR file offload).'),
    Modality(
        id='bp', label='Blood pressure (cuff)', category='event',
        clock='markers', tier='C',
        ts_file='markers.csv', ts_col='t_master_s',
        paths=('markers.csv (rating:BP:*)',),
        notes='Intermittent cuff readings entered by the operator, stored as '
              'master-clock markers (systolic/diastolic/hr).'),
    Modality(
        id='wifi_csi', label='WiFi CSI', category='raw',
        clock='device+pc_anchor', tier='B',
        ts_file='csi/csi_timestamped.csv', ts_col='pc_timestamp_s',
        expected_hz=(20.0, 60.0, 130.0),
        paths=('csi/csi_timestamped.csv', 'csi/csi_log.csv'),
        notes='ESP32 device tick drifts vs PC; csi_timestamped.csv is the PC-clock '
              'anchor used to place CSI on the master clock. Without it CSI cannot '
              'be aligned.'),
    Modality(
        id='ecg', label='ECG', category='raw',
        clock='per_sample_ts', tier='A',
        ts_file='ecg/ecg_log.csv', ts_col='timestamp_s',
        expected_hz=(120.0, 500.0, 1000.0),
        paths=('ecg/ecg_log.csv',),
        notes='Frontier X chest strap: records to its own app/RTC and is EXPORTED '
              'after the session, so it is aligned POST-HOC — preprocessing.ecg_align '
              'cross-correlates ECG R-peaks against the master-clock oximeter pleth '
              '(same heart = shared clock) to fit t_master=a*t_device+b, then rewrites '
              'ecg_log.csv on the master clock. (A live-streaming strap like the Polar '
              'H10 would instead use recorder.ecg_recorder; that path is retained but '
              'not used for the Frontier X.)'),
    Modality(
        id='emg', label='EMG', category='raw',
        clock='per_sample_ts', tier='A',
        ts_file='emg/emg_log.csv', ts_col='timestamp_s',
        expected_hz=(200.0, 1000.0, 2000.0),
        paths=('emg/emg_log.csv',),
        notes='Real channels only are written (ch0..); display-only synthetic '
              'channels are never persisted.'),
    Modality(
        id='gsr', label='GSR / EDA', category='raw',
        clock='per_sample_ts', tier='C',
        ts_file='gsr/gsr_log.csv', ts_col='timestamp_s',
        expected_hz=(10.0, 50.0, 120.0),
        paths=('gsr/gsr_log.csv',),
        notes='Skin conductance; slow signal, seconds-level timing is sufficient.'),
    Modality(
        id='oximeter', label='Pulse oximeter (PPG ground truth)', category='raw',
        clock='per_sample_ts', tier='A',
        ts_file='oximeter/oximeter_log.csv', ts_col='timestamp_s',
        expected_hz=(20.0, 60.0, 100.0),
        ground_truth=None,
        paths=('oximeter/oximeter_log.csv',),
        notes='Contec CMS50E. The pleth column is the ground-truth PPG waveform for '
              'rPPG; recorded at oximeter-priority to avoid starvation.'),
]

# id → Modality
BY_ID = {m.id: m for m in MODALITIES}

# The 11 conceptual modalities in the user's ordering (oximeter is the instrument
# behind rPPG's ground truth, not a separate conceptual modality).
CONCEPTUAL_ORDER = [
    'facial_video', 'audio', 'questionnaire', 'rppg', 'micro_expression',
    'thermal', 'bp', 'wifi_csi', 'ecg', 'emg', 'gsr',
]


def raw_modalities() -> List[Modality]:
    """Physically-captured streams (the ones with real files + a clock to verify)."""
    return [m for m in MODALITIES if m.category == 'raw']


def by_category(category: str) -> List[Modality]:
    return [m for m in MODALITIES if m.category == category]


def summary_table() -> str:
    """A compact human-readable table of the manifest (used by docs / --list)."""
    rows = [('id', 'label', 'cat', 'tier', 'clock', 'expected_hz')]
    for m in MODALITIES:
        hz = '' if not m.expected_hz else f'{m.expected_hz[0]:g}-{m.expected_hz[2]:g}'
        rows.append((m.id, m.label, m.category, m.tier, m.clock, hz))
    w = [max(len(r[c]) for r in rows) for c in range(len(rows[0]))]
    out = []
    for i, r in enumerate(rows):
        out.append('  '.join(c.ljust(w[j]) for j, c in enumerate(r)))
        if i == 0:
            out.append('  '.join('-' * w[j] for j in range(len(w))))
    return '\n'.join(out)


if __name__ == '__main__':
    print(summary_table())
