"""
Oximeter Handler — V7.0 CMS50E Protocol (Ultra-Stable 3-Thread Version)
======================================================================
Direct integration of the working standalone cms50e_reader.py.
Uses the exact CMS50E_V7Reader class, matching connection timeout, start sequence,
and synchronous read logic. No custom ring-buffers or in_waiting loops that differ
from the working solo logic.
"""

import csv
import os
import time
import threading
import queue as _queue
from typing import Optional, Tuple

import serial
import serial.tools.list_ports

from src.recorder.sensor_registry import SensorRegistry, SensorState

# V7.0 Protocol Constants
BAUD_RATE = 115200
CMD_REALTIME_START = bytes([0x7D, 0x81, 0xA1, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80])
CMD_REALTIME_STOP  = bytes([0x7D, 0x81, 0xA2, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80])

PKT_REALTIME = 0x01
PKT_SIZE = 9

# Backward-compat exports
TRIGGERS = [CMD_REALTIME_START]
SERIAL_CONFIGS = [(115200, serial.PARITY_NONE)]
PARITY_LABELS = {serial.PARITY_NONE: 'N'}


def parse_realtime_packet(packet: bytes) -> Optional[dict]:
    """Parse a 9-byte V7.0 real-time data packet (EXACT copy from solo script)."""
    if len(packet) < PKT_SIZE or packet[0] != PKT_REALTIME:
        return None
    
    b = [packet[i] & 0x7F for i in range(PKT_SIZE)]
    
    ppg = b[3]
    hr = b[5]
    spo2 = b[6]
    signal_ind = b[1]
    signal_str = b[2]
    bar_graph = b[4]
    pi_high = b[7]
    pi_low = b[8]
    
    searching = (spo2 >= 127 or hr == 0)
    
    return {
        "ppg": ppg,
        "hr": hr,
        "spo2": spo2 if not searching else 0,
        "signal_ind": signal_ind,
        "signal_str": signal_str,
        "bar_graph": bar_graph,
        "pi_high": pi_high,
        "pi_low": pi_low,
        "searching": searching,
        "raw": list(packet),
    }


class CMS50E_V7Reader:
    """CMS50E V7.0 protocol 3-threaded reader (ultra-stable, zero-latency)."""
    
    def __init__(self, port=None):
        self.port = port
        self.ser = None
        self._byte_q = _queue.Queue(maxsize=128)    # ~64 KB; drop stale bytes on overflow
        self._packet_q = _queue.Queue(maxsize=600)  # ~10 s at 60 Hz
        self._stop_evt = threading.Event()
        self._reader_thr = None
        self._parser_thr = None
        self._lock = threading.Lock()
        
    def find_port(self):
        """Auto-detect the CMS50E serial port."""
        if self.port:
            return True
        for p in serial.tools.list_ports.comports():
            if p.vid == 0x10C4 and p.pid == 0xEA60:
                self.port = p.device
                return True
        for p in serial.tools.list_ports.comports():
            if 'ttyUSB' in p.device or 'ttyACM' in p.device:
                self.port = p.device
                return True
        return False
        
    def connect(self):
        """Open serial connection and start background reading threads."""
        if not self.find_port():
            return False
        try:
            self.ser = serial.Serial(
                port=self.port,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0, # Non-blocking serial read!
                write_timeout=1,
            )
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            time.sleep(0.15)
            self.start_streaming()
            
            # Start background threads
            self._stop_evt.clear()
            self._byte_q = _queue.Queue(maxsize=128)
            self._packet_q = _queue.Queue(maxsize=600)
            
            self._reader_thr = threading.Thread(
                target=self._reader_thread_fn,
                name="oxi-serial-drainer",
                daemon=True,
            )
            self._parser_thr = threading.Thread(
                target=self._parser_thread_fn,
                name="oxi-packet-parser",
                daemon=True,
            )
            self._reader_thr.start()
            self._parser_thr.start()
            return True
        except Exception:
            return False
            
    def start_streaming(self):
        """Send the V7.0 real-time data start command."""
        if not self.ser:
            return
        with self._lock:
            try:
                self.ser.write(CMD_REALTIME_START)
                self.ser.flush()
            except Exception:
                pass
                
    def stop_streaming(self):
        """Send the V7.0 real-time data stop command."""
        if not self.ser:
            return
        with self._lock:
            try:
                self.ser.write(CMD_REALTIME_STOP)
                self.ser.flush()
            except Exception:
                pass
                
    def _reader_thread_fn(self):
        """Drains raw bytes from the serial port instantly (Thread 1)."""
        ser = self.ser
        bq = self._byte_q
        while not self._stop_evt.is_set():
            try:
                chunk = ser.read(512)
                if chunk:
                    try:
                        bq.put_nowait(chunk)
                    except _queue.Full:
                        pass  # drop stale bytes; real-time stream, old data is worthless
                else:
                    time.sleep(0.001) # Yield CPU 1ms
            except Exception:
                break
                
    def _parser_thread_fn(self):
        """Assembles and parses packets using a ring buffer (Thread 2)."""
        buf = bytearray()
        bq = self._byte_q
        pq = self._packet_q
        
        while not self._stop_evt.is_set():
            try:
                chunk = bq.get(timeout=0.1)
                buf.extend(chunk)
            except _queue.Empty:
                continue
                
            while len(buf) >= PKT_SIZE:
                idx = buf.find(PKT_REALTIME)
                if idx == -1:
                    buf.clear()
                    break
                if idx > 0:
                    del buf[:idx]
                if len(buf) < PKT_SIZE:
                    break
                raw = bytes(buf[:PKT_SIZE])
                parsed = parse_realtime_packet(raw)
                if parsed is not None:
                    del buf[:PKT_SIZE]
                    try:
                        pq.put_nowait(parsed)
                    except _queue.Full:
                        pass  # drop oldest parsed packet to prevent unbounded growth
                else:
                    del buf[:1]
                    
    def read_packet(self):
        """Return the next parsed packet from the queue (Main loop endpoint)."""
        try:
            return self._packet_q.get(timeout=0.02)
        except _queue.Empty:
            return None
            
    def close(self):
        """Stop streaming and stop threads."""
        self._stop_evt.set()
        if self.ser:
            self.stop_streaming()
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None


class OximeterHandler:
    def __init__(self, registry: SensorRegistry, sio, port_cfg='auto'):
        self.registry = registry
        self.sio = sio
        self.port_cfg = port_cfg
        self._stop = threading.Event()
        
        self.recording = False
        self.session_dir = None
        self.rec_start = None
        
        self._csv_f = None
        self._csv_w = None
        self._csv_flush_counter = 0
        
        # Non-blocking SocketIO emit queue
        self._emit_q = _queue.Queue(maxsize=120)
        self._emit_thread = threading.Thread(
            target=self._emit_worker, daemon=True, name='oxi-emit'
        )
        self._emit_thread.start()

    def _emit_worker(self):
        """Background thread that safely drains the SocketIO emit queue."""
        while True:
            try:
                event, data = self._emit_q.get(timeout=1.0)
                self.sio.emit(event, data)
            except _queue.Empty:
                if self._stop.is_set():
                    break
            except Exception:
                pass

    def _safe_emit(self, event: str, data: dict) -> None:
        """Enqueues status/data updates to prevent blocking the serial reader."""
        try:
            self._emit_q.put_nowait((event, data))
        except _queue.Full:
            pass

    @staticmethod
    def _find_port(port_cfg: str) -> Optional[str]:
        """Auto-detects CMS50E port (CP210x chip)."""
        if port_cfg and port_cfg not in ('auto', 'none'):
            return port_cfg
        for p in serial.tools.list_ports.comports():
            if p.vid == 0x10C4 and p.pid == 0xEA60:
                return p.device
        for p in serial.tools.list_ports.comports():
            if 'ttyUSB' in p.device or 'ttyACM' in p.device:
                return p.device
        return None

    def run(self):
        """Main loop: EXACT same lifecycle and reader as the working solo script."""
        self._stop.clear()
        while not self._stop.is_set():
            port = self._find_port(self.port_cfg)
            if not port:
                self.registry.set_state('oximeter', SensorState.SCANNING, 'No serial port found')
                self._safe_emit('device_status', {
                    'device': 'oximeter', 'ok': False,
                    'msg': 'No serial port — plug in CMS50E',
                })
                for _ in range(10):
                    if self._stop.is_set():
                        return
                    time.sleep(0.5)
                continue

            self.registry.set_state('oximeter', SensorState.SCANNING, f'Opening {port}...')
            self._safe_emit('device_status', {
                'device': 'oximeter', 'ok': False,
                'msg': f'Opening {port}...',
            })

            # Instantiate CMS50E_V7Reader exactly like solo script
            reader = CMS50E_V7Reader(port=port)
            if not reader.connect():
                self._safe_emit('device_status', {
                    'device': 'oximeter', 'ok': False,
                    'msg': f'Cannot open {port}',
                })
                time.sleep(3)
                continue

            status_msg = f'{port} @ {BAUD_RATE} 8N1 [V7.0]'
            self.registry.set_state('oximeter', SensorState.STREAMING, status_msg)
            self.registry.set_port('oximeter', port, BAUD_RATE, 'NONE')
            self.registry.set_protocol('oximeter', '9byte')
            self._safe_emit('device_status', {
                'device': 'oximeter', 'ok': True,
                'msg': f'✅ Locked: {status_msg}',
            })

            sample_count = 0
            counter_batch = 0
            last_keepalive = time.monotonic()

            try:
                while not self._stop.is_set():
                    now = time.monotonic()

                    # Keepalive matching solo script exactly (every 5 seconds)
                    if now - last_keepalive > 5.0:
                        reader.start_streaming()
                        last_keepalive = now

                    # Read packet using synchronous solo script logic
                    pkt = reader.read_packet()
                    if pkt is None:
                        continue

                    sample_count += 1
                    counter_batch += 1

                    # Batch updates to registry
                    if counter_batch >= 30:
                        self.registry.increment_counter('oximeter', counter_batch)
                        counter_batch = 0

                    sig = pkt.get('signal_str', 0)
                    spo2 = pkt['spo2']
                    hr = pkt['hr']
                    searching = pkt.get('searching', False)

                    # Continuous 60 Hz CSV logging (Write even if searching/no finger to prevent empty files)
                    if self.recording and self.session_dir:
                        if searching or sig == 0:
                            self._csv_write(0, 0, 0)
                        else:
                            self._csv_write(spo2, hr, sig)
                    elif self._csv_f is not None:
                        self._close_csv()

                    # UI/SocketIO display update (throttled)
                    if searching or sig == 0:
                        if sample_count % 60 == 0:
                            self._safe_emit('oxi_data', {
                                'spo2': 0, 'hr': 0, 'wave': 0,
                                'sig': 0, 'n': sample_count,
                            })
                    else:
                        if sample_count % 30 == 0:
                            self._safe_emit('oxi_data', {
                                'spo2': spo2, 'hr': hr,
                                'wave': 0,
                                'sig': min(sig, 7),
                                'n': sample_count,
                            })

            except Exception as e:
                self._safe_emit('device_status', {
                    'device': 'oximeter', 'ok': False,
                    'msg': f'Error: {str(e)[:80]}',
                })
            finally:
                if counter_batch > 0:
                    self.registry.increment_counter('oximeter', counter_batch)
                reader.close()
                self._close_csv()
                self.registry.set_state('oximeter', SensorState.DISCONNECTED)

            # Wait 3 seconds before trying to re-detect/re-open (interruptible)
            for _ in range(30):
                if self._stop.is_set():
                    return
                time.sleep(0.1)

    def _csv_write(self, spo2, pulse, sig):
        """Writes samples to log file."""
        if self._csv_f is None:
            p = os.path.join(self.session_dir, 'oximeter', 'oximeter_log.csv')
            is_new = not os.path.exists(p) or os.path.getsize(p) == 0
            self._csv_f = open(p, 'a', newline='', buffering=1)
            self._csv_w = csv.writer(self._csv_f)
            if is_new:
                self._csv_w.writerow([
                    'timestamp_s', 'spo2', 'heart_rate', 'signal_strength',
                ])
            self._csv_flush_counter = 0
        t = time.monotonic() - self.rec_start
        self._csv_w.writerow([f'{t:.4f}', spo2, pulse, sig])
        self._csv_flush_counter += 1
        if self._csv_flush_counter >= 10:
            self._csv_f.flush()
            self._csv_flush_counter = 0

    def _close_csv(self):
        """Close log file."""
        if self._csv_f:
            self._csv_f.flush()
            self._csv_f.close()
            self._csv_f = None
            self._csv_w = None

    def stop(self):
        self._stop.set()
