# 🔧 Hardware Setup Guide

## Overview

This guide covers the physical setup of all three recording devices.

---

## Device List

| # | Device | Role | Connection |
|---|--------|------|------------|
| 1 | USB Webcam | Face video capture | USB to PC |
| 2 | Contec CMS60D | Ground truth SpO2/HR | USB-Serial to PC |
| 3 | ESP32 #1 | WiFi CSI Transmitter | USB to PC (for power + flash) |
| 4 | ESP32 #2 | WiFi CSI Receiver | USB to PC (for power + data) |

---

## Physical Arrangement

```
        ┌──────────────────────────────┐
        │         SUBJECT              │
        │     (seated, facing          │
        │      camera & ESP32s)        │
        │                              │
        │  [Oximeter on finger]        │
        └──────────────────────────────┘
                    │
                    │  ~0.5-1.5m
                    │
        ┌───────────▼───────────────────┐
        │                               │
        │  📷 Camera    📶 ESP32-TX     │
        │  (center)    (left, ~30cm     │
        │               from camera)    │
        │                               │
        │              📶 ESP32-RX      │
        │             (right, ~30cm     │
        │              from TX)         │
        │                               │
        │  ═══════════════════════════  │
        │         DESK / TABLE          │
        └───────────────────────────────┘
```

### Placement Notes
- **Camera**: Directly facing the subject's face, at eye level if possible
- **ESP32 TX & RX**: Placed on the same desk, 20-50 cm apart, with the subject in between or in line-of-sight
- **Oximeter**: On the subject's finger (index or middle finger, non-dominant hand)
- **Distance**: Subject should be 0.5-1.5m from the camera and ESP32 pair

---

## Serial Port Identification

After connecting all USB devices, identify ports:

```bash
# List all serial ports
ls /dev/ttyUSB* /dev/ttyACM*

# Identify which port is which device
dmesg | grep tty

# Typical mapping:
# /dev/ttyUSB0 — Contec CMS60D Oximeter
# /dev/ttyUSB1 — ESP32 Receiver (CSI data)
```

---

## ESP32 Firmware Flashing

See `firmware/transmitter/` and `firmware/receiver/` for the ESP-IDF projects.

```bash
# Flash transmitter
cd firmware/transmitter
idf.py build
idf.py -p /dev/ttyUSB2 flash

# Flash receiver
cd firmware/receiver
idf.py build
idf.py -p /dev/ttyUSB1 flash
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Camera not detected | Check `ls /dev/video*`, try `v4l2-ctl --list-devices` |
| Oximeter no data | Verify baud rate (115200), check cable and probe |
| ESP32 no CSI output | Ensure TX is in station mode, RX in AP mode on same channel |
| Permission denied on serial | Add user to dialout group: `sudo usermod -aG dialout $USER` |
