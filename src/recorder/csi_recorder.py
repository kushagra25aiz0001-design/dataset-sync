"""
WiFi CSI Recorder — ESP32
==========================
Reads Channel State Information (CSI) data from the ESP32 receiver
via serial UART. Adapted from /home/jarvis/esp_26Feb/collect_data.py.

The receiver ESP32 outputs CSV lines like:
    timestamp_ms,rssi,len,amp[0],amp[1],...,amp[63]
or with a header comment starting with '#'.
"""

import os
import csv
import time
import threading

import serial


class CSIRecorder:
    """Records ESP32 WiFi CSI data with monotonic timestamps."""

    def __init__(self, session, port: str = "/dev/ttyUSB1", baud: int = 921600):
        self.session = session
        self.port = port
        self.baud = baud

        self.output_dir = session.csi_dir
        self.packet_count = 0
        self._stop_event = threading.Event()
        self._thread = None

        # Update session metadata
        session.update_device_config("csi", {
            "transmitter": "ESP32-TX",
            "receiver": "ESP32-RX",
            "port": port,
            "baud_rate": baud,
            "channel": 6,
            "bandwidth": "HT20",
            "packet_rate_hz": 100,
            "amplitude": "L2 (sqrt(I^2+Q^2))",
        })

    def start(self):
        """Start recording in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop recording and wait for thread to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    @staticmethod
    def _is_data_row(line: str) -> bool:
        """Check if line is a CSV data row (starts with digit)."""
        stripped = line.strip()
        return len(stripped) > 0 and stripped[0].isdigit()

    @staticmethod
    def _is_header_row(line: str) -> bool:
        """Check if line is a CSV header (starts with '#')."""
        return line.strip().startswith("#")

    def _record_loop(self):
        """Main recording loop — runs in thread."""
        try:
            ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=1.0
            )
        except serial.SerialException as e:
            print(f"[CSI] ERROR: Cannot open {self.port}: {e}")
            print(f"[CSI] Is the receiver ESP32 plugged in?")
            print(f"[CSI] Check: ls /dev/ttyUSB*")
            return

        print(f"[CSI] Connected to {self.port} @ {self.baud} baud")

        # Clear any stale data
        ser.reset_input_buffer()
        time.sleep(0.1)

        # Output file: raw CSI log (append mode for crash recovery)
        csi_path = os.path.join(self.output_dir, "csi_log.csv")
        out_file = open(csi_path, "a", buffering=1)
        header_saved = out_file.tell() > 0  # skip header if resuming

        # Also write a version with our timestamps prepended
        ts_path = os.path.join(self.output_dir, "csi_timestamped.csv")
        ts_file = open(ts_path, "a", newline="", buffering=1)
        ts_writer = csv.writer(ts_file)
        if ts_file.tell() == 0:
            ts_writer.writerow(["session_timestamp_s", "raw_line"])

        try:
            while not self._stop_event.is_set():
                try:
                    raw = ser.readline()
                except serial.SerialException:
                    continue

                if not raw:
                    continue

                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue

                if not line:
                    continue

                timestamp = time.monotonic() - self.session.t0

                # Save header row
                if self._is_header_row(line) and not header_saved:
                    out_file.write(line + "\n")
                    header_saved = True
                    continue

                # Save data rows
                if self._is_data_row(line):
                    out_file.write(line + "\n")
                    ts_writer.writerow([f"{timestamp:.4f}", line])
                    self.packet_count += 1

                    # Progress + flush every 500 packets (~17 s at 29 Hz)
                    if self.packet_count % 500 == 0:
                        print(f"\r[CSI] packets={self.packet_count:>6}  "
                              f"elapsed={timestamp:.1f}s", end="", flush=True)
                        out_file.flush()
                        ts_file.flush()

        except Exception as e:
            print(f"\n[CSI] ERROR: {e}")
        finally:
            ser.close()
            out_file.close()
            ts_file.close()
            print(f"\n[CSI] Stopped. {self.packet_count} packets recorded.")
