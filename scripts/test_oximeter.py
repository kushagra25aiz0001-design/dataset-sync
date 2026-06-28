#!/usr/bin/env python3
"""
CMS50E Pulse Oximeter — Continuous Data Reader v3.0 (FINAL)
============================================================
Device : Contec CMS50E
Protocol: V7.0 — 9-byte packets @ ~60 Hz
Baud   : 115200 / 8N1
 
WHY THIS VERSION EXISTS (root cause of freezes and data gaps):
--------------------------------------------------------------
Single-threaded readers mix serial reading with CSV writing, display
updates, and keep-alive logic in one loop. At 60 Hz the CMS50E sends
one 9-byte packet every ~16 ms. If ANY step in the loop takes longer
than that, the OS serial buffer fills, bytes overflow, sync is lost,
and you get a freeze or gap.
 
SOLUTION — 3-thread architecture:
----------------------------------
  Thread 1 (reader_thread) : ONLY drains bytes from the serial port
                             into a raw byte queue. Nothing else. Ever.
  Thread 2 (parser_thread) : Pulls raw bytes, assembles 9-byte packets,
                             emits parsed dicts to a packet queue.
  Main thread              : Takes packets, writes CSV, updates display.
                             Never touches the serial port directly.
 
The serial buffer is always drained faster than data arrives regardless
of what the main thread is doing. This is the only correct design for
continuous high-rate serial data capture.
 
USAGE:
  python scripts/test_oximeter.py                          # live display
  python scripts/test_oximeter.py --record                 # record to auto-named CSV
  python scripts/test_oximeter.py -o session.csv           # record to named file
  python scripts/test_oximeter.py -d 300 --record          # record 5 minutes
  python scripts/test_oximeter.py --port /dev/ttyUSB1      # force a specific port
  python scripts/test_oximeter.py --raw-dump               # debug hex dump
  python scripts/test_oximeter.py --record --reconnect     # auto-reconnect on USB drop
  python scripts/test_oximeter.py --record --csv-only      # silent background logging
"""
 
import argparse
import csv
import glob
import os
import queue
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
 
 
# ═══════════════════════════════════════════════════════════
#  Protocol constants
# ═══════════════════════════════════════════════════════════
 
BAUD_RATE = 115200
PKT_SIZE  = 9
PKT_SYNC  = 0x01   # every real-time packet starts with this byte
 
# V7.0 "start real-time stream" command
CMD_START = bytes([0x7D, 0x81, 0xA1, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80])
CMD_STOP  = bytes([0x7D, 0x81, 0xA2, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80])
 
KEEPALIVE_INTERVAL = 5.0   # seconds between keep-alive commands
STALL_TIMEOUT      = 8.0   # seconds without a packet before reconnect attempt
 
 
# ═══════════════════════════════════════════════════════════
#  Packet parser  (pure function, zero serial I/O)
# ═══════════════════════════════════════════════════════════
 
def parse_packet(raw: bytes) -> dict | None:
    """
    Parse one 9-byte V7.0 real-time packet.
 
    All bytes from the device have bit-7 set as a framing marker.
    Strip it with & 0x7F to get the actual 7-bit value.
 
    Byte map:
      [0] = 0x01  packet type (sync)
      [1] = signal status / probe flags
      [2] = signal strength  (0-15)
      [3] = PPG waveform amplitude  (0-127)
      [4] = bar-graph / probe status
      [5] = pulse rate  (bpm)
      [6] = SpO2  (%)   -- 127 means searching
      [7] = PI high byte
      [8] = PI low byte
    """
    if len(raw) < PKT_SIZE or raw[0] != PKT_SYNC:
        return None
 
    b = [raw[i] & 0x7F for i in range(PKT_SIZE)]
 
    spo2      = b[6]
    hr        = b[5]
    searching = (spo2 >= 127 or hr == 0)
 
    return {
        "ppg":       b[3],
        "hr":        hr,
        "spo2":      0 if searching else spo2,
        "sig_str":   b[2],
        "bar":       b[4],
        "pi_high":   b[7],
        "pi_low":    b[8],
        "searching": searching,
        "raw":       list(raw),
    }
 
 
# ═══════════════════════════════════════════════════════════
#  3-Thread CMS50E Reader
# ═══════════════════════════════════════════════════════════
 
class CMS50EReader:
    """
    Three-thread continuous reader for the CMS50E V7.0 protocol.
 
    Queues:
      _byte_q   — raw bytes  (serial → parser thread)
      _packet_q — parsed dicts (parser → main thread)
    """
 
    def __init__(self, port: str | None = None):
        try:
            import serial
            import serial.tools.list_ports
            self._ser_mod = serial
        except ImportError:
            print("ERROR: pyserial not installed.  Run: pip install pyserial")
            sys.exit(1)
 
        self.port        = port
        self._ser        = None
        self._byte_q     = queue.Queue()       # unlimited — never block the drainer
        self._packet_q   = queue.Queue()       # unlimited — never block the parser
        self._stop_evt   = threading.Event()
        self._reader_thr = None
        self._parser_thr = None
        self._lock       = threading.Lock()    # guards serial write
 
    # ── Port discovery ──────────────────────────────────────
 
    def _find_port(self) -> bool:
        if self.port:
            return True
        # Prefer CP210x (Silicon Labs) — the chip inside the CMS50E
        for p in self._ser_mod.tools.list_ports.comports():
            if p.vid == 0x10C4 and p.pid == 0xEA60:
                self.port = p.device
                print(f"  [+] Auto-detected CMS50E (CP210x) at {self.port}")
                return True
        # Fallback: first ttyUSB / ttyACM
        candidates = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
        if candidates:
            self.port = candidates[0]
            print(f"  [+] Using {self.port} (no CP210x found — fallback)")
            return True
        print("  [-] No serial ports found.")
        return False
 
    # ── Connection ──────────────────────────────────────────
 
    def connect(self) -> bool:
        if not self._find_port():
            return False
        try:
            self._ser = self._ser_mod.Serial(
                port          = self.port,
                baudrate      = BAUD_RATE,
                bytesize      = self._ser_mod.EIGHTBITS,
                parity        = self._ser_mod.PARITY_NONE,
                stopbits      = self._ser_mod.STOPBITS_ONE,
                # timeout=0 means read() returns IMMEDIATELY with whatever
                # bytes are in the OS buffer right now (non-blocking).
                # This is critical: the reader thread can spin at full speed
                # and drain the buffer without ever sleeping inside read().
                timeout       = 0,
                write_timeout = 1,
            )
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
            time.sleep(0.15)
            print(f"  [+] Serial opened: {self.port} @ {BAUD_RATE}/8N1  (timeout=0, non-blocking)")
            self._send_cmd(CMD_START)
            return True
        except Exception as e:
            print(f"  [-] Failed to open {self.port}: {e}")
            if "Permission" in str(e):
                print("      Fix: sudo usermod -aG dialout $USER  then log out/in")
            return False
 
    def _send_cmd(self, cmd: bytes):
        """Thread-safe write to the serial port."""
        if not self._ser:
            return
        with self._lock:
            try:
                self._ser.write(cmd)
                self._ser.flush()
            except Exception as e:
                print(f"  [!] Write error: {e}")
 
    # ── Thread 1: byte drainer ──────────────────────────────
 
    def _reader_thread_fn(self):
        """
        ONLY reads bytes from the serial port and puts them into _byte_q.
        Does absolutely nothing else. This ensures the OS buffer is always
        drained immediately and no data is ever lost due to overflow.
        """
        ser = self._ser
        bq  = self._byte_q
 
        while not self._stop_evt.is_set():
            try:
                # Read up to 512 bytes in one call (bulk drain)
                chunk = ser.read(512)
                if chunk:
                    bq.put(chunk)
                else:
                    # Buffer was empty — yield CPU for 1 ms
                    # At 115200 baud, 9 bytes arrive every ~0.8 ms
                    # so 1 ms sleep never causes more than ~1 packet delay
                    time.sleep(0.001)
            except Exception as e:
                if not self._stop_evt.is_set():
                    print(f"\n  [!] Reader thread error: {e}")
                    self._stop_evt.set()
                break
 
    # ── Thread 2: assembler + parser ───────────────────────
 
    def _parser_thread_fn(self):
        """
        Pulls raw byte chunks from _byte_q, assembles 9-byte packets
        using a ring buffer, parses them, and puts results in _packet_q.
        Never blocks longer than 0.1s waiting for bytes.
        """
        buf = bytearray()
        bq  = self._byte_q
        pq  = self._packet_q
 
        while not self._stop_evt.is_set():
            # Wait for bytes (timeout so we can check _stop_evt)
            try:
                chunk = bq.get(timeout=0.1)
                buf.extend(chunk)
            except queue.Empty:
                continue
 
            # Extract all complete packets currently in buf
            while len(buf) >= PKT_SIZE:
                # Find sync byte
                idx = buf.find(PKT_SYNC)
                if idx == -1:
                    buf.clear()
                    break
                if idx > 0:
                    del buf[:idx]       # drop garbage before sync
 
                if len(buf) < PKT_SIZE:
                    break               # packet not yet complete — wait
 
                raw    = bytes(buf[:PKT_SIZE])
                parsed = parse_packet(raw)
 
                if parsed is not None:
                    del buf[:PKT_SIZE]
                    pq.put(parsed)
                else:
                    # 0x01 was a data value, not a real sync byte
                    # Skip one byte and try again
                    del buf[:1]
 
    # ── Start / stop ────────────────────────────────────────
 
    def start(self):
        """Launch background threads."""
        self._stop_evt.clear()
 
        self._reader_thr = threading.Thread(
            target=self._reader_thread_fn,
            name="cms50e-serial-drainer",
            daemon=True,
        )
        self._parser_thr = threading.Thread(
            target=self._parser_thread_fn,
            name="cms50e-packet-parser",
            daemon=True,
        )
        self._reader_thr.start()
        self._parser_thr.start()
        print("  [+] Serial drainer and packet parser threads started")
 
    def stop(self):
        """Stop threads and close port."""
        self._stop_evt.set()
        try:
            if self._ser and self._ser.is_open:
                self._send_cmd(CMD_STOP)
                time.sleep(0.05)
                self._ser.close()
        except Exception:
            pass
        self._ser = None
 
    # ── Public API for main thread ──────────────────────────
 
    def get_packet(self, timeout: float = 0.05) -> dict | None:
        """Blocking get with timeout. Returns None on empty."""
        try:
            return self._packet_q.get(timeout=timeout)
        except queue.Empty:
            return None
 
    def queue_depth(self) -> int:
        """How many parsed packets are waiting in the queue."""
        return self._packet_q.qsize()
 
    def send_keepalive(self):
        self._send_cmd(CMD_START)
 
    def reconnect(self) -> bool:
        """Full USB reconnect — for when cable is pulled."""
        print("\n  [!] Attempting USB reconnect...")
        self.stop()
        time.sleep(2)
        self.port = None   # re-scan
        for attempt in range(12):
            if self.connect():
                self.start()
                print("  [+] Reconnected.")
                return True
            print(f"      Retry {attempt + 1}/12...")
            time.sleep(3)
        print("  [-] Could not reconnect after 12 attempts.")
        return False
 
 
# ═══════════════════════════════════════════════════════════
#  PPG ASCII waveform helper
# ═══════════════════════════════════════════════════════════
 
def ppg_wave(history, width: int = 40) -> str:
    if len(history) < 2:
        return " " * width
    samples = list(history)[-width:]
    lo, hi  = min(samples), max(samples)
    rng     = hi - lo or 1
    blocks  = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    wave    = "".join(
        blocks[max(0, min(8, int(((s - lo) / rng) * 8)))]
        for s in samples
    )
    return wave[-width:].rjust(width)
 
 
# ═══════════════════════════════════════════════════════════
#  Main recording / display loop
# ═══════════════════════════════════════════════════════════
 
def run(
    reader:         CMS50EReader,
    record_file:    str | None = None,
    duration:       int | None = None,
    auto_reconnect: bool       = False,
    csv_only:       bool       = False,
):
    csv_fh     = None
    csv_writer = None
 
    if record_file:
        os.makedirs(
            os.path.dirname(record_file) if os.path.dirname(record_file) else ".",
            exist_ok=True,
        )
        csv_fh     = open(record_file, "w", newline="")
        csv_writer = csv.writer(csv_fh)
        csv_writer.writerow([
            "timestamp_iso", "elapsed_s",
            "ppg", "heart_rate", "spo2",
            "signal_strength", "bar", "pi_high", "pi_low",
            "searching", "raw_hex",
        ])
        print(f"  [+] Recording to: {record_file}")
 
    t0          = time.monotonic()
    wall_start  = datetime.now()
    count       = 0
    last_disp   = 0.0
    last_alive  = t0
    last_packet = t0
    ppg_hist    = deque(maxlen=120)
 
    # Hz counter
    hz_n    = 0
    hz_last = t0
    cur_hz  = 0.0
 
    running = True
 
    def on_sig(sig, frame):
        nonlocal running
        running = False
 
    signal.signal(signal.SIGINT,  on_sig)
    signal.signal(signal.SIGTERM, on_sig)
 
    if not csv_only:
        print(f"\n  {'=' * 66}")
        print(f"  CMS50E LIVE  --  {wall_start.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  V7.0 Protocol  |  115200/8N1  |  target ~60 Hz")
        if duration:
            print(f"  Auto-stop after {duration}s")
        if auto_reconnect:
            print(f"  Auto-reconnect: ON")
        print(f"  Ctrl+C to stop")
        print(f"  {'=' * 66}\n")
 
    try:
        while running:
            now     = time.monotonic()
            elapsed = now - t0
 
            # Duration check
            if duration and elapsed >= duration:
                if not csv_only:
                    print(f"\n\n  Time limit reached ({duration}s). Stopping.")
                break
 
            # Keep-alive
            if now - last_alive >= KEEPALIVE_INTERVAL:
                reader.send_keepalive()
                last_alive = now
 
            # Auto-reconnect on stall
            if auto_reconnect and (now - last_packet) > STALL_TIMEOUT:
                if not reader.reconnect():
                    break
                last_packet = time.monotonic()
                last_alive  = last_packet
                continue
 
            # Get next packet from queue
            # timeout=0.02 means: wait at most 20ms for a packet.
            # At 60Hz a packet arrives every 16ms so this barely ever waits.
            pkt = reader.get_packet(timeout=0.02)
            if pkt is None:
                continue
 
            # -- Valid packet --
            count       += 1
            hz_n        += 1
            last_packet  = time.monotonic()
            ppg_hist.append(pkt["ppg"])
 
            ts_iso     = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            ts_elapsed = f"{elapsed:.4f}"
 
            # CSV
            if csv_writer:
                raw_hex = " ".join(f"{b:02X}" for b in pkt["raw"])
                csv_writer.writerow([
                    ts_iso,
                    ts_elapsed,
                    pkt["ppg"],
                    pkt["hr"],
                    pkt["spo2"],
                    pkt["sig_str"],
                    pkt["bar"],
                    pkt["pi_high"],
                    pkt["pi_low"],
                    int(pkt["searching"]),
                    raw_hex,
                ])
                # Flush every 30 packets (~0.5s at 60Hz)
                # Safe against abrupt exit, doesn't slow the loop down
                if count % 30 == 0:
                    csv_fh.flush()
 
            # Display — throttled to 10 updates/sec so terminal rendering
            # never slows down the main loop
            if csv_only:
                continue
 
            now2 = time.monotonic()
            if now2 - last_disp >= 0.1:
                last_disp = now2
 
                hz_elapsed = now2 - hz_last
                if hz_elapsed >= 1.0:
                    cur_hz  = hz_n / hz_elapsed
                    hz_n    = 0
                    hz_last = now2
 
                wave = ppg_wave(ppg_hist, width=40)
                qdep = reader.queue_depth()
 
                if pkt["searching"]:
                    status   = "\033[93m[!] NO FINGER / SEARCHING\033[0m"
                    hr_str   = "---"
                    spo2_str = "---"
                else:
                    status   = "\033[92m[*] Reading               \033[0m"
                    hr_str   = f"{pkt['hr']:>3d}"
                    spo2_str = f"{pkt['spo2']:>3d}"
 
                sys.stdout.write(
                    f"\r  {status}  "
                    f"HR:\033[1m{hr_str}\033[0mbpm  "
                    f"SpO2:\033[1m{spo2_str}\033[0m%  "
                    f"PPG:{pkt['ppg']:>3d}  "
                    f"\033[36m{wave}\033[0m  "
                    f"{cur_hz:>5.1f}Hz  "
                    f"#{count:<7,d}  [{elapsed:>7.1f}s]  Q:{qdep}   "
                )
                sys.stdout.flush()
 
    except KeyboardInterrupt:
        pass
 
    finally:
        total = time.monotonic() - t0
 
        if csv_fh:
            csv_fh.flush()
            csv_fh.close()
 
        if not csv_only:
            print(f"\n\n  {'=' * 66}")
            print(f"  SESSION SUMMARY")
            print(f"  {'=' * 66}")
            print(f"  Duration    : {total:.1f} s")
            print(f"  Packets     : {count:,}")
            if total > 0:
                print(f"  Avg rate    : {count / total:.1f} Hz")
            if record_file and os.path.exists(record_file):
                sz = os.path.getsize(record_file)
                print(f"  CSV         : {record_file}  ({sz/1024:.1f} KB)")
            print(f"  {'=' * 66}\n")
 
 
# ═══════════════════════════════════════════════════════════
#  Raw hex debug mode
# ═══════════════════════════════════════════════════════════
 
def raw_dump(reader: CMS50EReader, duration: int = 10):
    print(f"\n  RAW HEX DUMP  --  {duration}s")
    print("  " + "=" * 66)
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration:
        pkt = reader.get_packet(timeout=0.05)
        if pkt:
            hex_s   = " ".join(f"{b:02X}" for b in pkt["raw"])
            elapsed = time.monotonic() - t0
            print(
                f"  [{elapsed:>6.2f}s]  {hex_s}"
                f"  PPG={pkt['ppg']:>3d}  HR={pkt['hr']:>3d}  SpO2={pkt['spo2']:>3d}"
            )
    print("  " + "=" * 66)
 
 
# ═══════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════
 
def main():
    p = argparse.ArgumentParser(
        description="CMS50E Continuous Reader v3.0 (V7.0 Protocol, 3-thread)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/test_oximeter.py                          live display
  python scripts/test_oximeter.py --record                 auto-named CSV
  python scripts/test_oximeter.py -o my_data.csv           named CSV
  python scripts/test_oximeter.py -d 300 --record          5-minute recording
  python scripts/test_oximeter.py --port /dev/ttyUSB1      force port
  python scripts/test_oximeter.py --raw-dump               debug hex
  python scripts/test_oximeter.py --record --reconnect     auto-reconnect
  python scripts/test_oximeter.py --record --csv-only      silent logging
        """,
    )
    p.add_argument("--port",      "-p",  help="Serial port (default: auto)")
    p.add_argument("--record",    "-r",  action="store_true", help="Save to CSV")
    p.add_argument("--output",    "-o",  default=None,        help="CSV output path")
    p.add_argument("--duration",  "-d",  type=int, default=None, help="Seconds to run")
    p.add_argument("--raw-dump",         action="store_true", help="Debug hex dump")
    p.add_argument("--reconnect",        action="store_true", help="Auto-reconnect on USB drop")
    p.add_argument("--csv-only",         action="store_true", help="Silent mode (no terminal output)")
    args = p.parse_args()
 
    print("\n" + "=" * 70)
    print("  CMS50E Pulse Oximeter -- Continuous Reader v3.0  (V7.0 Protocol)")
    print("  3-thread: drainer / parser / recorder  --  115200/8N1  --  ~60 Hz")
    print("=" * 70)
 
    reader = CMS50EReader(port=args.port)
 
    if not reader.connect():
        print("\n  Cannot connect. Check:")
        print("  1. USB cable plugged in?")
        print("  2. CMS50E powered ON with finger on probe?")
        print("  3. Permission? -> sudo usermod -aG dialout $USER")
        sys.exit(1)
 
    reader.start()
 
    # Warmup: wait up to 5s for first packet
    print("  Waiting for first packet...", end=" ", flush=True)
    deadline = time.monotonic() + 5.0
    first    = None
    while time.monotonic() < deadline:
        first = reader.get_packet(timeout=0.1)
        if first:
            break
 
    if first:
        print(
            f"\033[92mOK\033[0m  "
            f"PPG={first['ppg']}  HR={first['hr']}  SpO2={first['spo2']}"
        )
        # Re-queue so run() counts it
        reader._packet_q.put(first)
    else:
        print("\033[93mWARNING: no data yet -- ensure finger is on probe\033[0m")
 
    try:
        if args.raw_dump:
            raw_dump(reader, duration=args.duration or 10)
        else:
            record_file = None
            if args.record or args.output:
                if args.output:
                    record_file = args.output
                else:
                    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
                    record_file = f"data/cms50e_{ts}.csv"
 
            run(
                reader,
                record_file    = record_file,
                duration       = args.duration,
                auto_reconnect = args.reconnect,
                csv_only       = args.csv_only,
            )
    finally:
        reader.stop()
        print("  Device closed.")
 
 
if __name__ == "__main__":
    main()
