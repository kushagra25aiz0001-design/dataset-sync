"""
Oximeter Recorder — Standalone CLI Recorder
============================================
Records Contec pulse oximeter data for CLI-based recording sessions
(via sync_manager.py). Uses the unified parser from the dashboard
handlers module.

This module is the CLI counterpart to the dashboard's OximeterHandler.
Both share the same packet parsing logic to ensure consistent behavior.

Usage:
    Used by sync_manager.py — not called directly.
"""

import os
import csv
import time
import threading
from typing import Optional, Tuple

import serial
import serial.tools.list_ports

from src.dashboard.handlers.oximeter_handler import (
    parse_5byte_packet, parse_9byte_packet, detect_protocol,
    TRIGGERS, SERIAL_CONFIGS, OXIMETER_VIDS, OXIMETER_PIDS,
    PARITY_LABELS,
)


class OximeterRecorder:
    """Records Contec pulse oximeter data with auto-protocol detection."""

    def __init__(self, session, port: str = "auto", baud: int = 0):
        """
        Args:
            session: Session object with t0 reference
            port: Serial port or "auto" to auto-detect
            baud: Baud rate or 0 to auto-detect
        """
        self.session = session
        self.requested_port = port
        self.requested_baud = baud
        self.output_dir = session.oximeter_dir
        self.sample_count = 0
        self._stop_event = threading.Event()
        self._thread = None

        # Detected settings
        self.active_port = None
        self.active_baud = None
        self.active_parity = None
        self.protocol = None

        session.update_device_config("oximeter", {
            "name": "Contec CMS50E",
            "port": port,
            "protocol": "auto-detect",
            "note": "Multi-protocol reader (5-byte/9-byte)"
        })

    @classmethod
    def find_oximeter_port(cls) -> Optional[str]:
        """Auto-detect the oximeter's serial port."""
        ports = serial.tools.list_ports.comports()
        for port in ports:
            if port.vid in OXIMETER_VIDS and port.pid in OXIMETER_PIDS:
                print(f"[OXIMETER] Found Silicon Labs device: {port.device} "
                      f"(VID={port.vid:04X} PID={port.pid:04X})")
                return port.device
            if port.description and any(kw in port.description.lower()
                                         for kw in ['cp210', 'silicon',
                                                    'contec', 'pulse']):
                print(f"[OXIMETER] Found device by description: "
                      f"{port.device} ({port.description})")
                return port.device

        tty_ports = [p.device for p in ports
                     if 'ttyUSB' in p.device or 'ttyACM' in p.device]
        if tty_ports:
            print(f"[OXIMETER] No known device found. "
                  f"Available ports: {tty_ports}")
            return tty_ports[0]

        return None

    def _try_protocol(self, port: str, baud: int, parity,
                      timeout: float = 3.0) -> Optional[str]:
        """Try to detect the protocol on a given port/baud/parity combo."""
        try:
            ser = serial.Serial(
                port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE, parity=parity, timeout=0.5,
            )
        except serial.SerialException:
            return None

        try:
            ser.reset_input_buffer()
            time.sleep(0.1)

            for trigger in TRIGGERS:
                try:
                    ser.write(trigger)
                    time.sleep(0.05)
                except Exception:
                    pass

            time.sleep(0.3)

            buf = bytearray()
            start = time.time()
            while time.time() - start < timeout:
                data = ser.read(ser.in_waiting or 64)
                if data:
                    buf.extend(data)
                if len(buf) >= 20:
                    protocol = detect_protocol(buf)
                    if protocol:
                        ser.close()
                        return protocol
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            try:
                ser.close()
            except Exception:
                pass

        return None

    def _auto_detect(self) -> Tuple[Optional[str], Optional[int],
                                     Optional[int], Optional[str]]:
        """Auto-detect port, baud, parity, and protocol."""
        if self.requested_port == "auto":
            port = self.find_oximeter_port()
            if not port:
                print("[OXIMETER] ERROR: No serial port found!")
                return None, None, None, None
        else:
            port = self.requested_port

        print(f"[OXIMETER] Probing {port}...")
        print("[OXIMETER] Make sure the device is ON and finger probe "
              "is attached!")
        print()

        if self.requested_baud > 0:
            configs = [
                (self.requested_baud, serial.PARITY_NONE),
                (self.requested_baud, serial.PARITY_EVEN),
                (self.requested_baud, serial.PARITY_ODD),
            ]
        else:
            configs = SERIAL_CONFIGS

        total = len(configs)
        for idx, (baud, parity) in enumerate(configs, 1):
            p_label = PARITY_LABELS.get(parity, '?')
            print(f"[OXIMETER]   [{idx}/{total}] Trying {baud} 8{p_label}1...",
                  end=" ", flush=True)

            protocol = self._try_protocol(port, baud, parity, timeout=4.0)
            if protocol:
                print(f"✅ FOUND! Protocol: {protocol}")
                return port, baud, parity, protocol
            else:
                print("no valid data")

        print()
        print("[OXIMETER] ❌ Could not detect protocol automatically.")
        print("[OXIMETER] Falling back to raw capture mode (9600, 8N1)...")
        return port, 9600, serial.PARITY_NONE, "raw"

    def start(self):
        """Start recording in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._record_loop, daemon=True,
        )
        self._thread.start()

    def stop(self):
        """Stop recording and wait for thread to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _record_loop(self):
        """Main recording loop with auto-detection."""
        port, baud, parity, protocol = self._auto_detect()

        if not port:
            print("[OXIMETER] Cannot proceed without a serial port.")
            return

        self.active_port = port
        self.active_baud = baud
        self.active_parity = parity
        self.protocol = protocol

        parity_str = "ODD" if parity == serial.PARITY_ODD else "NONE"
        self.session.update_device_config("oximeter", {
            "name": "Contec CMS50E",
            "port": port,
            "baud_rate": baud,
            "parity": parity_str,
            "protocol": protocol,
            "sample_rate_hz": 60,
        })

        try:
            ser = serial.Serial(
                port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE, parity=parity, timeout=1.0,
            )
        except serial.SerialException as e:
            print(f"[OXIMETER] ERROR: Cannot open {port}: {e}")
            return

        print(f"[OXIMETER] Recording with {protocol} protocol "
              f"on {port} @ {baud}")

        # Re-send triggers
        for trigger in TRIGGERS:
            try:
                ser.write(trigger)
                time.sleep(0.03)
            except Exception:
                pass
        time.sleep(0.2)
        ser.reset_input_buffer()

        # Record using unified parser
        self._record_with_parser(ser, protocol)

        ser.close()
        print(f"\n[OXIMETER] Stopped. {self.sample_count} samples recorded.")

    def _record_with_parser(self, ser, protocol: str):
        """
        Record using the shared parser functions.
        Handles both 5-byte and 9-byte protocols, plus raw fallback.
        """
        csv_path = os.path.join(self.output_dir, "oximeter_log.csv")
        csv_file = open(csv_path, "a", newline="", buffering=1)
        writer = csv.writer(csv_file)
        if csv_file.tell() == 0:
            writer.writerow([
                "timestamp_s", "spo2", "heart_rate", "pulse_waveform",
                "signal_strength", "searching", "beep",
            ])

        # For raw mode, also save binary data
        raw_file = None
        if protocol == "raw":
            raw_path = os.path.join(self.output_dir, "oximeter_raw.bin")
            raw_file = open(raw_path, "wb")

        buf = bytearray()
        detected_protocol = protocol if protocol != "raw" else None
        last_trigger = time.time()

        # Select parser function
        if protocol == "5byte":
            parse_fn = parse_5byte_packet
        elif protocol == "9byte":
            parse_fn = parse_9byte_packet
        else:
            parse_fn = None  # Will auto-detect

        try:
            while not self._stop_event.is_set():
                # Re-trigger in raw mode
                if (parse_fn is None and self.sample_count == 0
                        and time.time() - last_trigger > 5):
                    for trigger in TRIGGERS:
                        try:
                            ser.write(trigger)
                            time.sleep(0.03)
                        except Exception:
                            pass
                    last_trigger = time.time()

                data = ser.read(ser.in_waiting or 1)
                if not data:
                    if self.sample_count == 0 and parse_fn is not None:
                        for trigger in TRIGGERS[:3]:
                            try:
                                ser.write(trigger)
                            except Exception:
                                pass
                    continue

                if raw_file:
                    raw_file.write(data)
                buf.extend(data)

                # Auto-detect protocol in raw mode
                if parse_fn is None and len(buf) >= 18:
                    detected_protocol = detect_protocol(buf)
                    if detected_protocol == "5byte":
                        parse_fn = parse_5byte_packet
                    elif detected_protocol == "9byte":
                        parse_fn = parse_9byte_packet

                if parse_fn is None:
                    # Not enough data to detect yet
                    if len(buf) > 500:
                        del buf[:len(buf) - 100]
                    continue

                # Parse all available packets
                while True:
                    pkt = parse_fn(buf)
                    if pkt is None:
                        break

                    spo2 = pkt['spo2']
                    hr = pkt['hr']
                    sig = pkt.get('sig', 0)

                    if sig == 0:
                        self.sample_count += 1
                        continue

                    if 0 < spo2 <= 100 and 0 < hr < 300:
                        timestamp = time.monotonic() - self.session.t0
                        writer.writerow([
                            f"{timestamp:.4f}", spo2, hr,
                            pkt['wave'], sig,
                            int(pkt.get('searching', False)),
                            int(pkt.get('beep', False)),
                        ])
                        self.sample_count += 1

                        if self.sample_count % 300 == 0:
                            print(
                                f"\r[OXIMETER] samples={self.sample_count:>6}  "
                                f"SpO2={spo2}%  HR={hr} BPM  "
                                f"wave={pkt['wave']}",
                                end="", flush=True,
                            )
                            csv_file.flush()

        except Exception as e:
            print(f"\n[OXIMETER] ERROR: {e}")
        finally:
            csv_file.close()
            if raw_file:
                raw_file.close()
                raw_size = os.path.getsize(raw_file.name)
                if raw_size > 0:
                    print(f"\n[OXIMETER] Raw data saved: "
                          f"{raw_file.name} ({raw_size} bytes)")
