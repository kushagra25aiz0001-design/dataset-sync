# 🗺️ Project Roadmap

## Phase 1 — Repository Setup ✅

- [x] Create directory structure
- [x] Write README.md, ROADMAP.md, DATA_FORMAT.md
- [x] Add requirements.txt, setup.py, .gitignore
- [x] Create placeholder files for all modules

---

## Phase 2 — Hardware & Firmware Setup

- [ ] Flash ESP32 **transmitter** firmware (Station mode, sends CSI packets)
- [ ] Flash ESP32 **receiver** firmware (AP mode, captures CSI data via UART)
- [ ] Test Contec CMS60D **oximeter** serial communication (`/dev/ttyUSB*`)
- [ ] Verify camera access via OpenCV (`cv2.VideoCapture`)
- [ ] Document wiring and device placement in `HARDWARE_SETUP.md`

**Key References:**
- ESP32 CSI: [Espressif CSI Guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/wifi.html#wi-fi-channel-state-information)
- Contec CMS60D: Serial protocol at 115200 baud, 8N1

---

## Phase 3 — Synchronized Recording Pipeline

- [ ] `camera_recorder.py` — Capture video frames with timestamps
- [ ] `oximeter_recorder.py` — Read SpO2/HR packets from Contec CMS60D via pyserial
- [ ] `csi_recorder.py` — Parse CSI amplitude/phase from ESP32 serial output
- [ ] `sync_manager.py` — Start all 3 recorders simultaneously, shared clock
- [ ] `session.py` — Create session folder (`session_YYYYMMDD_HHMMSS`), write `metadata.json`

**Synchronization Strategy:**
1. All recorders share a reference `time.monotonic()` clock
2. Each data point is timestamped relative to session start
3. `metadata.json` stores start time, device configs, and session info

---

## Phase 4 — Data Collection

- [ ] Record pilot sessions (1-2 min each) to validate pipeline
- [ ] Record full sessions (5-10 min each) with varied:
  - Subjects (different skin tones, ages)
  - Distances (0.5m, 1m, 1.5m from camera/ESP32)
  - Lighting conditions (indoor, natural, dim)
  - Activity levels (resting, post-exercise)
- [ ] Aim for **50+ sessions** for a robust dataset

---

## Phase 5 — Preprocessing

- [ ] `video_preprocessor.py` — Face detection (MediaPipe), ROI extraction, skin pixel mean
- [ ] `csi_preprocessor.py` — Extract amplitude/phase from raw CSI, bandpass filter (0.7-4 Hz)
- [ ] `oximeter_preprocessor.py` — Parse SpO2/HR values, interpolate gaps
- [ ] `synchronizer.py` — Temporally align all 3 modalities to common timeline
- [ ] Save processed data to `data/processed/session_*/`

---

## Phase 6 — Dataset Construction

- [ ] `builder.py` — Segment continuous data into fixed-length windows (e.g., 10s)
- [ ] `splitter.py` — Subject-wise train/val/test split (70/15/15)
- [ ] `loader.py` — PyTorch Dataset & DataLoader with multi-modal batching
- [ ] Save final splits to `datasets/train/`, `datasets/val/`, `datasets/test/`

---

## Phase 7 — Model Training & Evaluation

### Models to Implement

| Model | Input | Output | Architecture |
|-------|-------|--------|-------------|
| Camera rPPG | Face video frames | BVP signal | CNN + LSTM |
| CSI rPPG | CSI amplitude/phase | BVP signal | 1D-CNN + BiLSTM |
| Fusion | Camera + CSI | BVP signal | Dual-stream + Attention |

### Training
- [ ] Implement training loop with early stopping
- [ ] Loss: Negative Pearson correlation + MSE
- [ ] Optimizer: AdamW, LR scheduler: CosineAnnealing

### Evaluation Metrics
- [ ] Mean Absolute Error (MAE) for HR estimation
- [ ] Root Mean Square Error (RMSE)
- [ ] Pearson correlation coefficient
- [ ] Bland-Altman analysis

---

## Phase 8 — Results & Documentation

- [ ] Generate evaluation plots (predicted vs ground truth, Bland-Altman)
- [ ] Write Jupyter notebooks for reproducible analysis
- [ ] Finalize all documentation
- [ ] (Optional) Prepare for publication / open-source release
