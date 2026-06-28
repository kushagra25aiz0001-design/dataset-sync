<div align="center">

# 📡 Dataset Sync

### Multi-Modal Synchronized Dataset for rPPG Signal Prediction

**Camera** · **Contec CMS60D Oximeter** · **ESP32 WiFi CSI**

[![Python 3.9+](https://img.shields.io/badge/Python-3.9+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![PyTorch](https://img.shields.io/badge/PyTorch-2.0+-EE4C2C?style=for-the-badge&logo=pytorch&logoColor=white)](https://pytorch.org)
[![ESP32](https://img.shields.io/badge/ESP32-CSI-000000?style=for-the-badge&logo=espressif&logoColor=white)](https://www.espressif.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge)](LICENSE)

</div>

---

## 🎯 Overview

**Dataset Sync** is a research framework for collecting synchronized multi-modal physiological data to predict **remote photoplethysmography (rPPG)** signals. It combines three recording modalities:

| Modality | Device | Purpose |
|----------|--------|---------|
| 🎥 **Camera** | Webcam / USB Camera | Capture facial video for rPPG extraction |
| 💓 **Pulse Oximeter** | Contec CMS60D | Ground truth SpO2 & heart rate via serial |
| 📶 **WiFi CSI** | ESP32 (Tx + Rx) | Channel State Information for contactless sensing |

All three modalities are **recorded simultaneously** with shared timestamps, enabling training of models that predict cardiovascular signals from **non-contact** sensors (camera + WiFi CSI) using the oximeter as **ground truth**.

---

## 📁 Repository Structure

```
Dataset_Sync/
├── config/                    # Device & model configuration
│   ├── recording_config.yaml  # Camera FPS, serial port, CSI settings
│   └── model_config.yaml      # Hyperparameters, architecture settings
│
├── docs/                      # Documentation
│   ├── ROADMAP.md             # Step-by-step project roadmap
│   ├── HARDWARE_SETUP.md      # Wiring & device placement guide
│   ├── DATA_FORMAT.md         # Data schema per modality
│   └── CONTRIBUTING.md        # Contribution guidelines
│
├── firmware/                  # ESP32 firmware
│   ├── transmitter/           # CSI transmitter (Station mode)
│   └── receiver/              # CSI receiver (AP mode + logger)
│
├── src/                       # Python source code
│   ├── recorder/              # Synchronized recording pipeline
│   ├── preprocessing/         # Data cleaning & alignment
│   ├── dataset/               # ML-ready dataset builders
│   └── models/                # rPPG prediction models
│
├── data/                      # Recorded data
│   ├── raw/                   # Raw session recordings
│   └── processed/             # Cleaned & aligned data
│
├── datasets/                  # Train/Val/Test splits
├── models/                    # Saved checkpoints
├── notebooks/                 # Jupyter notebooks
├── scripts/                   # CLI utility scripts
├── tests/                     # Unit & integration tests
└── results/                   # Evaluation outputs
```

---

## 🛠️ Hardware Requirements

| Component | Specification |
|-----------|--------------|
| **PC** | Linux (Ubuntu 20.04+) with USB ports |
| **Camera** | Any USB webcam (720p+ recommended) |
| **Oximeter** | Contec CMS60D with USB-Serial cable |
| **ESP32 × 2** | One transmitter + one receiver for WiFi CSI |
| **USB Cables** | For ESP32 boards and oximeter |

See [docs/HARDWARE_SETUP.md](docs/HARDWARE_SETUP.md) for wiring diagrams and placement guidelines.

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/your-username/Dataset_Sync.git
cd Dataset_Sync
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### 2. Configure Devices

Edit `config/recording_config.yaml` with your device ports and settings.

### 3. Record a Session

```bash
python -m src.recorder.sync_manager --duration 60 --subject "subject_01"
```

This creates a synchronized session under `data/raw/session_YYYYMMDD_HHMMSS/` with `camera/`, `oximeter/`, and `csi/` subfolders.

### 4. Preprocess

```bash
python -m src.preprocessing.synchronizer --session data/raw/session_YYYYMMDD_HHMMSS
```

### 5. Train Model

```bash
python -m src.models.train --config config/model_config.yaml
```

---

## 📊 Data Flow

```
┌─────────────┐    ┌─────────────────┐    ┌──────────────┐    ┌──────────────┐
│  📷 Camera   │    │  💓 Oximeter     │    │  📶 ESP32    │    │              │
│  (30 FPS)   │───▶│  (Contec CMS60D)│───▶│  (WiFi CSI)  │───▶│  Sync Mgr    │
│  USB/Webcam │    │  USB-Serial     │    │  UART/Serial │    │  (Timestamps)│
└─────────────┘    └─────────────────┘    └──────────────┘    └──────┬───────┘
                                                                      │
                                                            ┌─────────▼─────────┐
                                                            │  data/raw/         │
                                                            │  session_YYYYMMDD/ │
                                                            │  ├── camera/       │
                                                            │  ├── oximeter/     │
                                                            │  ├── csi/          │
                                                            │  └── metadata.json │
                                                            └─────────┬─────────┘
                                                                      │
                                                            ┌─────────▼─────────┐
                                                            │  Preprocessing    │
                                                            │  ├── Face ROI     │
                                                            │  ├── CSI Parse    │
                                                            │  └── Temporal     │
                                                            │      Alignment    │
                                                            └─────────┬─────────┘
                                                                      │
                                                            ┌─────────▼─────────┐
                                                            │  Model Training   │
                                                            │  ├── Camera rPPG  │
                                                            │  ├── CSI rPPG     │
                                                            │  └── Fusion Model │
                                                            └───────────────────┘
```

---

## 🗺️ Roadmap

| Phase | Description | Status |
|-------|-------------|--------|
| **1** | Repository structure & documentation | ✅ Done |
| **2** | Hardware setup & ESP32 firmware | ⬜ Planned |
| **3** | Synchronized recording pipeline | ⬜ Planned |
| **4** | Data collection (multi-session) | ⬜ Planned |
| **5** | Preprocessing & alignment | ⬜ Planned |
| **6** | Dataset construction (train/val/test) | ⬜ Planned |
| **7** | Model training & evaluation | ⬜ Planned |
| **8** | Results, figures & documentation | ⬜ Planned |

See [docs/ROADMAP.md](docs/ROADMAP.md) for detailed breakdown.

---

## 📄 License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

---

<div align="center">

**Built with ❤️ for contactless physiological sensing research**

</div>
