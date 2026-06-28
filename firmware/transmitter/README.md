# CSI Transmitter – ESP32

## Purpose
Continuously broadcasts small **ESP-NOW** frames so a second ESP32
(**csi_receiver**) can collect **Channel State Information (CSI)** data
from the Wi-Fi signal. CSI captures how the signal changes as it
travels between the two boards – useful for detecting subtle body
movements such as heartbeats (see [nickbild/csi_hr](https://github.com/nickbild/csi_hr)).

---

## Hardware
| Item | Notes |
|------|-------|
| ESP32-WROOM-32 based board | DevKitC, HUZZAH32, etc. |
| USB–Serial cable | Any CP2102 / CH340 adapter |

Place the transmitter **several feet away** from the receiver.
The subject sits/stands **between** the two boards.

---

## Project Structure
```
csi_transmitter/
├── CMakeLists.txt            ← top-level project cmake
├── sdkconfig.defaults        ← critical Wi-Fi/FreeRTOS settings
└── main/
    ├── CMakeLists.txt        ← component cmake
    ├── idf_component.yml     ← IDF version constraint (>= 4.4.1)
    └── app_main.c            ← all application code
```

---

## Key Configuration (inside `app_main.c`)

| `#define` | Default | Description |
|-----------|---------|-------------|
| `CONFIG_LESS_INTERFERENCE_CHANNEL` | `6` | Wi-Fi channel (must match receiver) |
| `CONFIG_SEND_FREQUENCY` | `100` | Packets per second |
| `CONFIG_WIFI_BANDWIDTH` | `WIFI_BW_HT20` | 20 MHz → 64 CSI subcarriers |
| `CONFIG_CSI_SEND_MAC` | `1a:00:00:00:00:00` | Fixed MAC so receiver can filter our frames |

> **Both boards must use the same channel and bandwidth!**

---

## Prerequisites (Linux)

### 1. Install ESP-IDF (if not already installed)
```bash
mkdir -p ~/esp && cd ~/esp
git clone --recursive https://github.com/espressif/esp-idf.git
cd ~/esp/esp-idf
git checkout v5.1           # or v5.2 / v5.3 – any >= 4.4.1 works
./install.sh esp32
```

### 2. Source the environment (run once per terminal session)
```bash
source ~/esp/esp-idf/export.sh
```

---

## Build & Flash

```bash
cd /home/jarvis/esp_26Feb/csi_transmitter

# Set target chip
idf.py set-target esp32

# (Optional) Open config menu to review settings
# idf.py menuconfig

# Build + Flash + Monitor  (change /dev/ttyUSB0 to your port)
idf.py build flash -b 921600 -p /dev/ttyUSB0 monitor
```

### Check serial permissions
If you get a _permission denied_ error on `/dev/ttyUSB0`:
```bash
sudo usermod -aG dialout $USER
# Log out and back in, then retry
```

---

## Expected Serial Output
After flashing you should see something like:
```
I (312) csi_send: ================ CSI SEND ================
I (318) csi_send: channel: 6  frequency: 100 Hz  mac: 1a:00:00:00:00:00
```
After that, the board loops silently sending 100 packets/second.

---

## Next Step
Flash the **csi_receiver** firmware on the second ESP32 board and
connect it to your Linux PC via USB to collect CSI data.
