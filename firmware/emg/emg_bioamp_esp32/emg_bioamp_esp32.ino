/*
 * EMG Sensor Reader — ESP32 Firmware
 * ===================================
 * Sensor: BioAmp EXG Pill (Upside Down Labs)
 *         https://github.com/upsidedownlabs/BioAmp-EXG-Pill
 * Board:  ESP32 DevKit (any variant with ADC1)
 *
 * Wiring (with voltage divider for 5V-powered BioAmp):
 *   BioAmp VCC  → ESP32 3.3V  (or 5V with divider on OUT)
 *   BioAmp GND  → ESP32 GND
 *   BioAmp OUT  → 2.2kΩ → ESP32 GPIO 34 (ADC1_CH6)
 *                          └─ 1kΩ → GND
 *   (If powering BioAmp from 3.3V, connect OUT directly to GPIO 34)
 *
 * Electrode placement (EMG):
 *   IN+  → Target muscle (e.g. forearm, bicep)
 *   IN-  → Same muscle, ~2cm from IN+
 *   REF  → Bony area (back of hand, elbow, wrist bone)
 *
 * Output: Binary packets over Serial @ 230400 baud
 *         Protocol matches Dataset Sync dashboard auto-detection:
 *
 *   Byte 0:     0xC7  (sync byte 1)
 *   Byte 1:     0x7C  (sync byte 2)
 *   Bytes 2-3:  CH0   (raw EMG, 12-bit, big-endian uint16)
 *   Bytes 4-5:  CH1   (bandpass-filtered EMG)
 *   Bytes 6-7:  CH2   (rectified / envelope)
 *   Bytes 8-33: CH3–CH15 (reserved, zero-filled)
 *   Bytes 34:   checksum low byte (sum of bytes 2-33)
 *   Bytes 35:   0xFF  (end marker)
 *
 *   Total: 36 bytes per packet @ 250 Hz = 9000 bytes/sec
 *   Fits comfortably in 230400 baud (~23 kB/sec capacity)
 *
 * Sample rate: 250 Hz (4ms per sample) — matches typical EMG requirements
 */

// ===================== USER CONFIG =====================
// ADC input pin — must be on ADC1 (GPIO 32–39 on ESP32)
// GPIO 34 is recommended (input-only, no internal pull-up noise)
#define EMG_PIN            34

// Sample rate
#define SAMPLE_RATE_HZ     250
#define SAMPLE_INTERVAL_US (1000000 / SAMPLE_RATE_HZ)  // 4000 µs

// Serial baud — must match dashboard's emg_baud (default: 230400)
#define SERIAL_BAUD        230400

// Number of channels in the binary packet (dashboard expects 16)
#define NUM_CHANNELS       16
// ========================================================

// ─── Binary Packet ───────────────────────────────────────
// Sync bytes that the dashboard's _probe_port_protocol() looks for
#define SYNC_BYTE_1  0xC7
#define SYNC_BYTE_2  0x7C
#define PACKET_SIZE  36   // 2 sync + 16×2 data + 1 checksum + 1 end

uint8_t packet[PACKET_SIZE];

// ─── Digital Bandpass Filter (74.5 – 149.5 Hz) ──────────
// 4th-order IIR Butterworth bandpass, designed for Fs = 500 Hz
// Adapted from Upside Down Labs EMGFilter for 250 Hz sample rate
// Coefficients recomputed for Fs=250 Hz, passband 74.5–149.5 Hz
// Using cascaded 2nd-order sections (biquads)

// Biquad filter state
struct Biquad {
  float b0, b1, b2, a1, a2;
  float z1, z2;  // delay elements
};

// Bandpass 74.5–149.5 Hz @ 250 Hz sample rate
// Designed as two cascaded biquad sections
Biquad bpf1 = {
  // Section 1: bandpass
   0.2929,  0.0,    -0.2929,   // b0, b1, b2
  -0.1716,  0.4142,            // a1, a2
   0.0,     0.0                // z1, z2 (initial state)
};

Biquad bpf2 = {
  // Section 2: bandpass
   0.2929,  0.0,    -0.2929,
   0.3420, -0.4142,
   0.0,     0.0
};

// 50 Hz notch filter (to reject powerline interference)
Biquad notch50 = {
   0.9025,  -0.4990,  0.9025,
  -0.4990,   0.8050,
   0.0,      0.0
};

float biquad_process(Biquad &f, float x) {
  float y = f.b0 * x + f.z1;
  f.z1 = f.b1 * x - f.a1 * y + f.z2;
  f.z2 = f.b2 * x - f.a2 * y;
  return y;
}

// ─── Envelope Detector ───────────────────────────────────
// Simple exponential moving average of rectified signal
float envelope = 0;
#define ENVELOPE_ATTACK  0.05   // fast rise (muscle contraction)
#define ENVELOPE_DECAY   0.005  // slow fall (muscle relaxation)

// ─── Timing ──────────────────────────────────────────────
unsigned long lastSampleTime = 0;
unsigned long sampleCount = 0;

// ─── Setup ───────────────────────────────────────────────
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(200);

  // Configure ADC
  analogReadResolution(12);        // 12-bit: 0–4095
  analogSetAttenuation(ADC_11db);  // Full range ~0–3.3V
  pinMode(EMG_PIN, INPUT);

  // Pre-fill packet with static bytes
  memset(packet, 0, PACKET_SIZE);
  packet[0] = SYNC_BYTE_1;
  packet[1] = SYNC_BYTE_2;
  packet[PACKET_SIZE - 1] = 0xFF;  // end marker

  // Print boot banner (non-JSON, dashboard will ignore these lines)
  Serial.println();
  Serial.println("========================================");
  Serial.println("  EMG BioAmp EXG Pill — ESP32 Firmware");
  Serial.println("  Protocol: Binary 0xC7 0x7C (36-byte)");
  Serial.printf ("  Sample rate: %d Hz\n", SAMPLE_RATE_HZ);
  Serial.printf ("  Baud: %d\n", SERIAL_BAUD);
  Serial.printf ("  ADC pin: GPIO %d\n", EMG_PIN);
  Serial.println("========================================");
  Serial.println("[EMG] Ready. Streaming data...");
  Serial.println();

  lastSampleTime = micros();
}

// ─── Main Loop ───────────────────────────────────────────
void loop() {
  unsigned long now = micros();

  // Fixed-rate sampling using micros() for accurate timing
  if ((now - lastSampleTime) < SAMPLE_INTERVAL_US) return;
  lastSampleTime += SAMPLE_INTERVAL_US;

  // ── 1. Read raw ADC ──
  int raw = analogRead(EMG_PIN);  // 0–4095

  // ── 2. Center the signal around zero (remove DC offset) ──
  // ESP32 ADC mid-point is ~2048 for a centered biopotential signal
  float centered = (float)raw - 2048.0;

  // ── 3. Apply 50 Hz notch filter (powerline rejection) ──
  float notched = biquad_process(notch50, centered);

  // ── 4. Apply bandpass filter (74.5 – 149.5 Hz) ──
  float filtered = biquad_process(bpf1, notched);
  filtered = biquad_process(bpf2, filtered);

  // ── 5. Compute envelope (rectify + smooth) ──
  float rectified = fabs(filtered);
  if (rectified > envelope) {
    envelope = envelope + ENVELOPE_ATTACK * (rectified - envelope);
  } else {
    envelope = envelope + ENVELOPE_DECAY * (rectified - envelope);
  }

  // ── 6. Scale back to uint16 for transmission ──
  // Raw: already 0–4095 (12-bit)
  uint16_t ch0_raw = (uint16_t)raw;

  // Filtered: re-center around 2048, clamp to 0–4095
  int filtered_int = (int)(filtered + 2048.0);
  if (filtered_int < 0) filtered_int = 0;
  if (filtered_int > 4095) filtered_int = 4095;
  uint16_t ch1_filtered = (uint16_t)filtered_int;

  // Envelope: scale to 0–4095 range (envelope is typically 0–500)
  int env_int = (int)(envelope * 8.0);  // ×8 for visibility
  if (env_int > 4095) env_int = 4095;
  uint16_t ch2_envelope = (uint16_t)env_int;

  // ── 7. Pack binary packet ──
  // CH0 = raw
  packet[2] = (ch0_raw >> 8) & 0xFF;
  packet[3] = ch0_raw & 0xFF;

  // CH1 = filtered
  packet[4] = (ch1_filtered >> 8) & 0xFF;
  packet[5] = ch1_filtered & 0xFF;

  // CH2 = envelope
  packet[6] = (ch2_envelope >> 8) & 0xFF;
  packet[7] = ch2_envelope & 0xFF;

  // CH3–CH15 remain zero (already memset in setup)

  // Compute checksum (sum of data bytes 2–33, low byte)
  uint8_t checksum = 0;
  for (int i = 2; i < PACKET_SIZE - 2; i++) {
    checksum += packet[i];
  }
  packet[PACKET_SIZE - 2] = checksum;

  // ── 8. Transmit ──
  Serial.write(packet, PACKET_SIZE);

  sampleCount++;
}
