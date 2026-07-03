# 📐 Data Format Specification

This document defines the data schema for every modality recorded during a
session, and — crucially — **how they all share one clock** so they can be
aligned afterward.

The single source of truth for the modality list is
[`src/recorder/modalities.py`](../src/recorder/modalities.py); the audit that
verifies a session against it is
[`src/preprocessing/sync_audit.py`](../src/preprocessing/sync_audit.py).

---

## The master clock (read this first)

Every captured stream is placed on **one clock**: `time.monotonic()` sampled once
at recording start (`rec_start`, a.k.a. `t0`). Each stream stamps

```
timestamp_s = time.monotonic() - rec_start        # seconds since start, ≈ 0 at t0
```

Three streams can't use a per-sample `timestamp_s` column and instead anchor to
the same origin a different way:

| Stream | How it lands on the master clock |
|--------|----------------------------------|
| **WiFi CSI** | ESP32 device tick drifts vs the PC, so `csi_timestamped.csv` pairs each packet with `pc_timestamp_s = monotonic − rec_start`. Without this file CSI **cannot** be aligned. |
| **Audio** | One anchor: `audio_meta.json → t_start_master_s = monotonic − rec_start` at the first captured sample; every later sample is `t_start + n/samplerate`. |
| **Markers** (questionnaire, BP, task events) | `markers.csv → t_master_s = monotonic − rec_start`, stamped on receipt. |

Because they share `rec_start`, "are they in sync?" reduces to checkable facts:
first timestamp ≈ 0 (same origin, not a wall-clock epoch), timestamps monotonic,
rate in band, no large gaps, spans overlap. Run:

```bash
python -m src.preprocessing.sync_audit --session data/raw/session_YYYYMMDD_HHMMSS
python -m src.preprocessing.sync_audit --all       # audit every session
```

It writes `data/processed/<session_id>/sync_audit.json` and prints a per-modality
table with an overall **PASS / WARN / FAIL**.

---

## The 11 modalities

| # | Modality | Kind | On disk | Clock | Tier |
|---|----------|------|---------|-------|------|
| 1 | Facial video (RGB) | raw | `camera/timestamps.csv` + `recording.avi` | per-sample | A |
| 2 | Audio | raw | `audio/audio.wav` + `audio_meta.json` | anchor | A |
| 3 | Text questionnaire | event | `markers.csv` + `responses.jsonl` | markers | C |
| 4 | rPPG | **derived** from #1 | `data/processed/**/rgb_signal.csv` | inherits #1 | A |
| 5 | Micro-expressions | **derived** from #1 | (offline) | inherits #1 | A |
| 6 | Thermal camera | raw | `thermal/timestamps.csv` + `frame_*.png` | per-sample | B |
| 7 | Blood pressure | event | `markers.csv` (`rating:BP:*`) | markers | C |
| 8 | WiFi CSI | raw | `csi/csi_timestamped.csv` + `csi_log.csv` | device+PC anchor | B |
| 9 | ECG | raw | `ecg/ecg_log.csv` | per-sample | A |
| 10 | EMG | raw | `emg/emg_log.csv` | per-sample | A |
| 11 | GSR / EDA | raw | `gsr/gsr_log.csv` | per-sample | C |

`oximeter/oximeter_log.csv` (Contec CMS50E) is also recorded — its `pleth` column
is the **ground-truth PPG** that rPPG (#4) is trained/validated against.

rPPG and micro-expressions are **not sensed separately**; they are computed from
the facial video, so their timing is the camera's timing. The questionnaire and
BP are **events**, not continuous streams, so they live in the marker log.

### Session directory

```
data/raw/session_YYYYMMDD_HHMMSS/
├── camera/     timestamps.csv (frame_idx,timestamp_s,filename,is_real) + recording.avi
├── audio/      audio.wav + audio_meta.json (t_start_master_s, samplerate, n_frames)
├── thermal/    timestamps.csv (frame_idx,timestamp_s,filename,is_real) + frame_*.png
├── oximeter/   oximeter_log.csv (timestamp_s,spo2,heart_rate,signal_strength,pleth)
├── csi/        csi_log.csv (device clock) + csi_timestamped.csv (pc_timestamp_s,raw_line)
├── ecg/        ecg_log.csv (timestamp_s,…)          [capture handler pending HW spec]
├── emg/        emg_log.csv (timestamp_s,ch0,ch1,…)  real channels only
├── gsr/        gsr_log.csv (timestamp_s,uS,raw,stress,zscore)
├── markers.csv  (t_master_s,label,source,t_device_s,offset_s,payload_json)
├── responses.jsonl
└── metadata.json
```

---

## Per-modality schemas

### 1. Facial video — `camera/timestamps.csv`

| Column | Type | Description |
|--------|------|-------------|
| `frame_idx` | int | Frame index (0-based) |
| `timestamp_s` | float | `monotonic − rec_start` |
| `filename` | str | Frame/logical name |
| `is_real` | 0/1 | **1** = genuinely captured; **0** = FRC-inserted duplicate with a synthetic timestamp |

> rPPG and micro-expressions must use **`is_real == 1` rows only** — duplicates
> carry no true capture time. The audit reports the duplicate fraction.

### 2. Audio — `audio/audio_meta.json`

```json
{ "t_start_master_s": 0.051, "samplerate": 16000, "channels": 1, "n_frames": 320000 }
```
`audio.wav` is 16-bit PCM. Sample *n* is at `t_start_master_s + n/samplerate`. The
mic is **recorder-owned** (same process/clock as the physiology) — never the
participant device.

### 6. Thermal — `thermal/timestamps.csv`

Same schema as the RGB camera; frames are **lossless PNG**, **real only** (no
duplication → `is_real` always `1`). Captured **live on the PC** so each frame is
on the master clock at grab time — from either a UVC/V4L2 node or a screen-shared
window:

```bash
--thermal-source /dev/video2               # UVC / USB thermal stream
--thermal-source screen:100,50,640,480     # crop of a screen-shared FLIR C5 window
```

Caveat: a live feed is **palette-colorized**, not absolute-°C radiometric (that
needs offline FLIR file offload).

### 8. WiFi CSI — `csi/csi_timestamped.csv`

| Column | Type | Description |
|--------|------|-------------|
| `pc_timestamp_s` | float | `monotonic − rec_start` (the alignable clock) |
| `raw_line` | str | `device_ms,seq,rssi,n_carriers,amp0,…,amp[n-1]` |

Amplitudes are the **last `n_carriers`** columns (HT20=64 / HT40=128). The audit
also fits the device-tick↔PC drift (ppm) and residual as a health metric.

### 3 / 7. Markers — `markers.csv`

| Column | Description |
|--------|-------------|
| `t_master_s` | `monotonic − rec_start`, stamped on receipt |
| `label` | taxonomy: `block_start:*`, `stim_onset:*`, `cue:*`, `posture:*`, `response:*`, `rating:<scale>:*`, `flash:n`, … |
| `source` | `backend` / `recorder` / `desktop` / `ipad` / `screen` |
| `t_device_s`, `offset_s` | present only for participant-device-origin events |
| `payload_json` | e.g. ratings, `rt_ms`, `correct` |

Blood pressure is stored as `rating:BP:<label>` with a `{systolic,diastolic,pulse}`
payload (see `BpReadTask`). The questionnaire is `rating:*` / `response:*` /
`questionnaire_done:*`.

---

## Syncing a *separate* task interface (external app)

The stimulus/task app runs as its own process (decoupled from the recorder) and
lines up by **POSTing each event** to the recorder's marker-ingest endpoint, which
stamps it on the master clock at receipt. Start the daemon with a port:

```bash
python -m src.recorder.headless_daemon --subject S01 --duration 600 --marker-port 8181
```

From the task app (browser / iPad / phone), at every boundary:

```js
fetch('http://<recorder-host>:8181/mark', {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    label: 'stim_onset:clip1',      // taxonomy label
    t_device_s: performance.now()/1000,  // the app's own clock → offset is fit offline
    source: 'ipad', trial: 4        // any extra keys become payload
  })
});
```

The reply is `{ok, t_master_s, offset_s}`. Because `t_device_s` is stored next to
the master-clock time, `sync_markers.estimate_offset` recovers the device↔master
offset/drift and removes the small network latency. `GET /time` returns the
current master-clock time (for NTP-style checks); `GET /health` returns
recording/session state. If no session is active, `/mark` returns HTTP **409** —
the app can fire freely without tracking recording state. This is preferred over
aligning by OCR-ing an on-screen counter (still possible as a fallback via the
same `estimate_offset` path).

## Audio from the camera (a6000 over HDMI)

To lock audio to the video, capture the a6000's audio embedded in the HDMI
capture-card stream (same hardware clock as the frames) instead of a separate mic:

```bash
python -m src.recorder.headless_daemon ... --audio --audio-device hdmi
python -m src.recorder.headless_daemon --list-audio-devices    # find the exact device
```

`--audio-device hdmi` auto-selects the capture card by name; `audio_meta.json`
records the chosen `device`. (A dedicated USB mic is still supported — better
speech quality, at the cost of a small measured anchor offset.)

## Processed / aligned output

`python -m src.preprocessing.synchronizer` resamples all present modalities onto a
uniform grid (default 30 Hz), writing `data/processed/<id>/aligned.csv` (+ `.npz`)
and folding markers into held `label_block/label_stim/label_cue/label_posture`
columns. The **audit** answers *"is it in sync?"*; the **synchronizer** produces
the aligned matrix once it is.
