# 📐 Data Format Specification

This document defines the data schema for each modality recorded during a session.

---

## Session Structure

Each recording session is stored under `data/raw/session_YYYYMMDD_HHMMSS/`:

```
session_20260227_221900/
├── camera/
│   ├── frame_000000.jpg       # Individual frames (if saving frames)
│   ├── frame_000001.jpg
│   ├── ...
│   ├── video.mp4              # Or a single video file
│   └── timestamps.csv         # Frame timestamps
├── oximeter/
│   └── oximeter_log.csv       # SpO2, HR, waveform data
├── csi/
│   └── csi_log.csv            # CSI amplitude & phase data
└── metadata.json              # Session metadata
```

---

## 1. Camera Data

### `camera/timestamps.csv`

| Column | Type | Description |
|--------|------|-------------|
| `frame_idx` | int | Frame index (0-based) |
| `timestamp_s` | float | Time since session start (seconds) |
| `filename` | str | Corresponding frame filename |

### Video Settings
- **Resolution:** 640×480 or 1280×720
- **FPS:** 30 (configurable)
- **Format:** MJPEG or H264 (.mp4)
- **Color space:** BGR (OpenCV default)

---

## 2. Oximeter Data (Contec CMS60D)

### `oximeter/oximeter_log.csv`

| Column | Type | Description |
|--------|------|-------------|
| `timestamp_s` | float | Time since session start (seconds) |
| `spo2` | int | Blood oxygen saturation (%) |
| `heart_rate` | int | Pulse rate (BPM) |
| `pulse_waveform` | int | Plethysmograph waveform value (0-127) |
| `signal_strength` | int | Signal quality indicator |
| `probe_connected` | bool | Finger probe status |

### Protocol Details
- **Baud rate:** 115200
- **Data bits:** 8, Stop bits: 1, Parity: None
- **Packet format:** 5-byte packets at ~60 Hz
- **Serial port:** `/dev/ttyUSB0` (typical on Linux)

---

## 3. WiFi CSI Data (ESP32)

### `csi/csi_log.csv`

| Column | Type | Description |
|--------|------|-------------|
| `timestamp_s` | float | Time since session start (seconds) |
| `mac` | str | Source MAC address |
| `rssi` | int | Received Signal Strength (dBm) |
| `rate` | int | Data rate |
| `noise_floor` | int | Noise floor (dBm) |
| `channel` | int | WiFi channel |
| `num_subcarriers` | int | Number of CSI subcarriers |
| `csi_data` | str | JSON array of [amplitude, phase] pairs |

### CSI Settings
- **WiFi standard:** 802.11n (HT20)
- **Bandwidth:** 20 MHz → 52 subcarriers (usable)
- **Packet rate:** ~100 Hz (configurable)
- **Format:** Each subcarrier has amplitude (float) and phase (float, radians)

---

## 4. Session Metadata

### `metadata.json`

```json
{
    "session_id": "session_20260227_221900",
    "start_time_utc": "2026-02-27T16:49:00Z",
    "start_time_local": "2026-02-27T22:19:00+05:30",
    "duration_seconds": 60,
    "subject_id": "subject_01",
    "subject_info": {
        "age": null,
        "gender": null,
        "skin_tone": null
    },
    "devices": {
        "camera": {
            "name": "USB Camera",
            "resolution": [640, 480],
            "fps": 30,
            "format": "mjpeg"
        },
        "oximeter": {
            "name": "Contec CMS60D",
            "port": "/dev/ttyUSB0",
            "baud_rate": 115200,
            "sample_rate_hz": 60
        },
        "csi": {
            "transmitter": "ESP32-TX",
            "receiver": "ESP32-RX",
            "port": "/dev/ttyUSB1",
            "channel": 6,
            "bandwidth": "HT20",
            "packet_rate_hz": 100
        }
    },
    "environment": {
        "location": "indoor",
        "lighting": "fluorescent",
        "distance_m": 1.0,
        "notes": ""
    }
}
```

---

## 5. Processed Data Format

After preprocessing, aligned data is saved under `data/processed/session_*/`:

### `labels.csv` (Ground Truth)

| Column | Type | Description |
|--------|------|-------------|
| `timestamp_s` | float | Aligned timestamp |
| `heart_rate_bpm` | float | Interpolated HR from oximeter |
| `spo2` | float | Interpolated SpO2 |
| `bvp_signal` | float | Blood Volume Pulse (normalized) |

All modalities are resampled to a **common sampling rate** (e.g., 30 Hz) and temporally aligned.
