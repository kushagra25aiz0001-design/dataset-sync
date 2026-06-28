"""
CSI Handler — WiFi Channel State Information (Asynchronous decoupled version)
==============================================================================
Reads CSI data from an ESP32 receiver via serial UART.
Parses CSV-formatted subcarrier amplitude data and emits
to the dashboard via SocketIO.

Data Format (from ESP32 receiver):
    timestamp_ms, packet_id, rssi, pad, pad, pad, sc[0], sc[1], ..., sc[51]

Each line is a comma-separated row. Lines starting with '#' are headers.
"""

import csv
import os
import random
import threading
import time
import queue as _queue

import serial

from src.recorder.sensor_registry import SensorRegistry, SensorState


class CSIHandler:
    """
    Dashboard CSI handler with reconnection support.

    Reads CSV lines from the ESP32 CSI receiver, parses subcarrier
    amplitudes and RSSI, and emits visualization data via SocketIO.
    """

    def __init__(self, registry: SensorRegistry, sio,
                 port='/dev/ttyUSB1', baud=921600):
        self.registry = registry
        self.sio = sio
        self.port = port
        self.baud = baud
        self._stop = threading.Event()

        # Non-blocking SocketIO emit queue
        self._emit_q = _queue.Queue(maxsize=120)
        self._emit_thread = threading.Thread(
            target=self._emit_worker, daemon=True, name='csi-emit'
        )
        self._emit_thread.start()

        # Recording state (set externally)
        self.recording = False
        self.session_dir = None
        self.rec_start = None

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
        """Enqueues status/data updates to prevent blocking."""
        try:
            self._emit_q.put_nowait((event, data))
        except _queue.Full:
            pass

    def run(self):
        """
        Main CSI monitoring loop. Call from a dedicated thread.
        """
        self._stop.clear()
        if not self.port or self.port == 'none':
            self.registry.set_state('csi', SensorState.DISABLED)
            return

        try:
            ser = serial.Serial(
                port=self.port, baudrate=self.baud, timeout=0, # Non-blocking!
            )
        except serial.SerialException as e:
            self.registry.set_state('csi', SensorState.ERROR, str(e))
            self._safe_emit('device_status', {
                'device': 'csi', 'ok': False, 'msg': str(e),
            })
            return

        self.registry.set_state('csi', SensorState.STREAMING,
                                f'{self.port} @ {self.baud}')
        self._safe_emit('device_status', {
            'device': 'csi', 'ok': True,
            'msg': f'{self.port} @ {self.baud}',
        })
        ser.reset_input_buffer()

        line_q = _queue.Queue(maxsize=300)  # ~10 s at 29 Hz; drop stale lines on overflow
        stop_reader = threading.Event()

        def reader_thread_fn():
            buf = bytearray()
            while not stop_reader.is_set():
                try:
                    chunk = ser.read(1024)
                    if chunk:
                        buf.extend(chunk)
                        while b'\n' in buf:
                            idx = buf.index(b'\n')
                            line = buf[:idx].decode('utf-8', errors='replace').strip()
                            del buf[:idx+1]
                            if line:
                                try:
                                    line_q.put_nowait(line)
                                except _queue.Full:
                                    pass  # drop stale CSI lines; real-time stream
                    else:
                        time.sleep(0.001)
                except Exception:
                    break

        reader_thread = threading.Thread(target=reader_thread_fn, daemon=True, name="csi-serial-drainer")
        reader_thread.start()

        csv_f = None       # raw ESP32 stream (device clock)
        ts_f = None        # PC-clock anchor: csi_timestamped.csv
        ts_w = None
        local_pkts = 0
        csv_flush_counter = 0

        try:
            while not self._stop.is_set():
                try:
                    line = line_q.get(timeout=0.1)
                except _queue.Empty:
                    continue

                if not line or not line[0].isdigit():
                    continue

                local_pkts += 1
                self.registry.set_counter('csi', local_pkts)

                # Parse amplitudes for visualization (with dynamic auto-healing interpolation for any null subcarriers).
                # Amplitudes are the trailing n_carriers (col 3) columns, so the
                # start index is len(parts) - n_carriers — robust to HT20/HT40 and
                # correct (the old hard-coded 6 dropped the first 2 subcarriers).
                parts = line.split(',')
                if len(parts) > 4:
                    try:
                        n_carriers = int(float(parts[3]))
                        amp_start = len(parts) - n_carriers
                        if amp_start < 4:
                            amp_start = 4
                        raw_amps = [abs(float(x)) for x in parts[amp_start:]]
                        if len(raw_amps) >= 52:
                            amps = raw_amps[:52]
                            
                            # Auto-heal any 0.0 or dead subcarriers dynamically
                            for i in range(len(amps)):
                                if amps[i] <= 1.0:
                                    # Find nearest active neighbor on the left
                                    left_val, left_idx = None, -1
                                    for l in range(i - 1, -1, -1):
                                        if amps[l] > 1.0:
                                            left_val = amps[l]
                                            left_idx = l
                                            break
                                    
                                    # Find nearest active neighbor on the right
                                    right_val, right_idx = None, -1
                                    for r in range(i + 1, len(amps)):
                                        if amps[r] > 1.0:
                                            right_val = amps[r]
                                            right_idx = r
                                            break
                                    
                                    # Interpolate
                                    noise = random.uniform(-0.8, 0.8)
                                    if left_val is not None and right_val is not None:
                                        frac = (i - left_idx) / float(right_idx - left_idx)
                                        amps[i] = max(0.0, left_val + frac * (right_val - left_val) + noise)
                                    elif left_val is not None:
                                        amps[i] = max(0.0, left_val + noise)
                                    elif right_val is not None:
                                        amps[i] = max(0.0, right_val + noise)
                                    else:
                                        amps[i] = max(0.0, 20.0 + noise)
                        else:
                            amps = raw_amps[:52]
                    except (ValueError, IndexError):
                        amps = []
                else:
                    amps = []

                # Parse RSSI
                try:
                    rssi_val = int(parts[2]) if len(parts) > 2 else 0
                except (ValueError, IndexError):
                    rssi_val = 0

                # Emit every 10th packet to avoid flooding SocketIO
                if local_pkts % 10 == 0:
                    self._safe_emit('csi_data', {
                        'amps': amps[:52],
                        'rssi': rssi_val,
                        'n': local_pkts,
                    })

                # Recording. csi_log.csv keeps the raw ESP32 stream (device clock);
                # csi_timestamped.csv pairs each line with a PC monotonic timestamp so
                # the device clock can be fit to the shared PC clock post-hoc
                # (pc_t = a*device_ms + b), which is the only way to align CSI to the
                # other sensors. The ESP32 tick alone drifts and resets on power-cycle.
                if self.recording and self.session_dir:
                    if csv_f is None:
                        csi_dir = os.path.join(self.session_dir, 'csi')
                        csv_f = open(os.path.join(csi_dir, 'csi_log.csv'),
                                     'a', buffering=1)
                        ts_f = open(os.path.join(csi_dir, 'csi_timestamped.csv'),
                                    'a', newline='', buffering=1)
                        ts_w = csv.writer(ts_f)
                        if ts_f.tell() == 0:
                            ts_w.writerow(['pc_timestamp_s', 'raw_line'])
                    pc_t = time.monotonic() - self.rec_start
                    csv_f.write(line + '\n')
                    ts_w.writerow([f'{pc_t:.4f}', line])
                    csv_flush_counter += 1
                    if csv_flush_counter >= 100:
                        csv_f.flush()
                        ts_f.flush()
                        csv_flush_counter = 0
                elif csv_f is not None:
                    csv_f.close()
                    csv_f = None
                    if ts_f is not None:
                        ts_f.close()
                        ts_f = None
                        ts_w = None
                    csv_flush_counter = 0

        except Exception as e:
            self._safe_emit('device_status', {
                'device': 'csi', 'ok': False, 'msg': str(e),
            })
        finally:
            stop_reader.set()
            try:
                reader_thread.join(timeout=1.0)
            except Exception:
                pass
            ser.close()
            if csv_f:
                csv_f.close()
            if ts_f:
                ts_f.close()
            self.registry.set_state('csi', SensorState.DISCONNECTED)

    def stop(self):
        """Signal the monitoring loop to stop."""
        self._stop.set()
