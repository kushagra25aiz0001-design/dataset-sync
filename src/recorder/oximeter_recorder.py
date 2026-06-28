"""
Oximeter Recorder — Standalone CLI Recorder
============================================
Records Contec CMS50E pulse-oximeter data for CLI recording sessions
(via sync_manager.py). Shares the exact V7.0 reader and packet parser used by
the live dashboard (src/dashboard/handlers/oximeter_handler.py) so both paths
behave identically and produce the same schema — including the `pleth` waveform.

Usage:
    Used by sync_manager.py — not called directly.
"""

import os
import csv
import time
import threading
from typing import Optional

import serial.tools.list_ports

from src.dashboard.handlers.oximeter_handler import (
    CMS50E_V7Reader, BAUD_RATE,
)

# Silicon Labs CP210x USB-serial bridge used by the CMS50E.
OXIMETER_VIDS = [0x10C4]
OXIMETER_PIDS = [0xEA60]


class OximeterRecorder:
    """Records Contec CMS50E data using the V7.0 real-time protocol."""

    def __init__(self, session, port: str = "auto", baud: int = 0):
        """
        Args:
            session: Session object exposing t0, oximeter_dir, update_device_config
            port: Serial port or "auto" to auto-detect
            baud: Unused (the V7 protocol is fixed at 115200 8N1); kept for the
                  sync_manager call signature.
        """
        self.session = session
        self.requested_port = port
        self.output_dir = session.oximeter_dir
        self.sample_count = 0
        self._stop_event = threading.Event()
        self._thread = None
        self.active_port = None

        session.update_device_config("oximeter", {
            "name": "Contec CMS50E",
            "port": port,
            "protocol": "v7-realtime",
            "baud_rate": BAUD_RATE,
        })

    @classmethod
    def find_oximeter_port(cls) -> Optional[str]:
        """Auto-detect the oximeter's serial port (CP210x VID/PID, else any tty)."""
        ports = list(serial.tools.list_ports.comports())
        for p in ports:
            if p.vid in OXIMETER_VIDS and p.pid in OXIMETER_PIDS:
                print(f"[OXIMETER] Found CP210x device: {p.device}")
                return p.device
        for p in ports:
            if p.description and any(k in p.description.lower()
                                     for k in ('cp210', 'silicon', 'contec', 'pulse')):
                print(f"[OXIMETER] Found by description: {p.device} ({p.description})")
                return p.device
        ttys = [p.device for p in ports
                if 'ttyUSB' in p.device or 'ttyACM' in p.device]
        if ttys:
            print(f"[OXIMETER] No known device; trying {ttys[0]}")
            return ttys[0]
        return None

    def start(self):
        """Start recording in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop recording and wait for the thread to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _record_loop(self):
        """Open the V7 reader and stream packets to CSV until stopped."""
        port = (self.find_oximeter_port()
                if self.requested_port in (None, "auto") else self.requested_port)
        if not port:
            print("[OXIMETER] ERROR: no serial port found.")
            return

        reader = CMS50E_V7Reader(port=port)
        if not reader.connect():
            print(f"[OXIMETER] ERROR: cannot open {port}")
            return
        self.active_port = port
        self.session.update_device_config("oximeter", {
            "name": "Contec CMS50E", "port": port,
            "protocol": "v7-realtime", "baud_rate": BAUD_RATE,
            "sample_rate_hz": 60,
        })
        print(f"[OXIMETER] Recording (V7.0) on {port} @ {BAUD_RATE}")

        csv_path = os.path.join(self.output_dir, "oximeter_log.csv")
        is_new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
        csv_file = open(csv_path, "a", newline="", buffering=1)
        writer = csv.writer(csv_file)
        if is_new:
            writer.writerow(["timestamp_s", "spo2", "heart_rate",
                             "signal_strength", "pleth"])

        last_keepalive = time.monotonic()
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if now - last_keepalive > 5.0:
                    reader.start_streaming()      # keepalive
                    last_keepalive = now

                pkt = reader.read_packet()
                if pkt is None:
                    continue

                searching = pkt.get("searching", False)
                sig = pkt.get("signal_str", 0)
                spo2 = 0 if (searching or sig == 0) else pkt.get("spo2", 0)
                hr = 0 if (searching or sig == 0) else pkt.get("hr", 0)
                ppg = pkt.get("ppg", 0)          # pleth waveform — always recorded

                t = time.monotonic() - self.session.t0
                writer.writerow([f"{t:.4f}", spo2, hr, sig, ppg])
                self.sample_count += 1
                if self.sample_count % 300 == 0:
                    csv_file.flush()
                    print(f"\r[OXIMETER] samples={self.sample_count:>6}  "
                          f"SpO2={spo2}%  HR={hr}", end="", flush=True)
        except Exception as e:
            print(f"\n[OXIMETER] ERROR: {e}")
        finally:
            reader.close()
            csv_file.flush()
            csv_file.close()
            print(f"\n[OXIMETER] Stopped. {self.sample_count} samples recorded.")
