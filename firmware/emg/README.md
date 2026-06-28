# EMG Firmware — BioAmp EXG Pill + ESP32

ESP32 firmware for the [BioAmp EXG Pill](https://github.com/upsidedownlabs/BioAmp-EXG-Pill) 
analog front-end from Upside Down Labs. Streams EMG data over USB Serial using the 
binary protocol auto-detected by the Dataset Sync dashboard.

## Wiring

### If powering BioAmp from 3.3V (simplest)

| BioAmp Pin | ESP32 Pin | Notes |
|------------|-----------|-------|
| VCC        | 3.3V      | Direct connection |
| GND        | GND       | Common ground |
| OUT        | GPIO 34   | Direct connection (ADC1_CH6) |

### If powering BioAmp from 5V (better signal quality)

| BioAmp Pin | Connection | Notes |
|------------|------------|-------|
| VCC        | 5V (USB)   | Higher voltage = better SNR |
| GND        | GND        | Common ground |
| OUT        | 2.2kΩ resistor → GPIO 34 | Voltage divider required! |
|            | GPIO 34 → 1kΩ → GND     | Protects ESP32 ADC (max 3.3V) |

### Electrode Placement (EMG)

```
 IN+  ────→  Target muscle (e.g. forearm flexor)
 IN-  ────→  Same muscle, ~2cm from IN+
 REF  ────→  Bony area (back of hand / wrist / elbow)
```

## Flashing

### Arduino IDE

1. Install ESP32 board support: `File > Preferences > Additional Board Manager URLs`:
   ```
   https://dl.espressif.com/dl/package_esp32_index.json
   ```
2. `Tools > Board > ESP32 Dev Module`
3. `Tools > Upload Speed > 921600`
4. `Tools > Port > /dev/ttyUSBx` (your ESP32 port)
5. Open `emg_bioamp_esp32.ino` and click Upload

### PlatformIO (alternative)

```bash
pio run -t upload --upload-port /dev/ttyUSBx
```

## Binary Protocol

The firmware outputs 36-byte binary packets at 250 Hz over Serial at **230400 baud**:

```
Byte  0:     0xC7        (sync byte 1)
Byte  1:     0x7C        (sync byte 2)
Bytes 2-3:   CH0         (raw EMG, 12-bit, big-endian uint16)
Bytes 4-5:   CH1         (bandpass filtered EMG)
Bytes 6-7:   CH2         (rectified envelope)
Bytes 8-33:  CH3–CH15    (reserved, zero)
Byte  34:    checksum    (sum of bytes 2-33, low byte)
Byte  35:    0xFF        (end marker)
```

The dashboard auto-detects this by scanning for the `0xC7 0x7C` sync pattern.

## Integration with Dataset Sync

Just plug in the ESP32 after flashing. The dashboard will:
1. Probe the serial port during startup discovery
2. Detect the `0xC7 0x7C` binary protocol
3. Identify it as EMG and claim the port
4. Show live 16-channel bar chart on the dashboard
5. Record to `emg/emg_log.csv` during sessions
