"""
GSR Handler — Galvanic Skin Response / Skin Conductance (Asynchronous decoupled version)
========================================================================================
Reads GSR data from an ESP32 + Sichiray sensor via serial UART.

Data Format (JSON lines from ESP32):
    {"uS": 2.34, "raw": 1856, "stress": 42, "zScore": 0.87}

    - uS:     Skin conductance in microsiemens
    - raw:    Raw ADC reading (0-4095)
    - stress: Stress percentage (0-100, computed on ESP32)
    - zScore: Statistical z-score of current reading
"""

import csv
import json
import os
import threading
import time
import queue as _queue

import serial

from src.recorder.sensor_registry import SensorRegistry, SensorState


class GSRHandler:
    """
    Dashboard GSR handler with auto-detection and reconnection.

    Uses port assigned by the SensorOrchestrator (via registry).
    Parses JSON-line data from the ESP32 GSR firmware.
    """

    def __init__(self, registry: SensorRegistry, sio,
                 port='auto', baud=115200):
        self.registry = registry
        self.sio = sio
        self.port_cfg = port
        self.baud = baud
        self._stop = threading.Event()

        # Non-blocking SocketIO emit queue
        self._emit_q = _queue.Queue(maxsize=120)
        self._emit_thread = threading.Thread(
            target=self._emit_worker, daemon=True, name='gsr-emit'
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

    def _get_port(self) -> str:
        """Get the assigned port from config or registry."""
        if self.port_cfg and self.port_cfg not in ('auto', 'none'):
            return self.port_cfg
        info = self.registry.get_sensor('gsr')
        if info and info.port:
            return info.port
        return None

    def run(self):
        """
        Main GSR monitoring loop. Call from a dedicated thread.
        Handles reconnection on disconnect.
        """
        self._stop.clear()
        if self.port_cfg == 'none':
            self.registry.set_state('gsr', SensorState.DISABLED)
            return

        while not self._stop.is_set():
            port = self._get_port()
            if not port:
                self.registry.set_state(
                    'gsr', SensorState.SCANNING,
                    'Waiting for port assignment...',
                )
                self._safe_emit('device_status', {
                    'device': 'gsr', 'ok': False,
                    'msg': 'Scanning for GSR device...',
                })
                for _ in range(10):
                    if self._stop.is_set():
                        return
                    time.sleep(0.5)
                continue

            # Open connection
            try:
                ser = serial.Serial(
                    port=port, baudrate=self.baud, timeout=0, # Non-blocking!
                )
            except serial.SerialException as e:
                self.registry.set_state('gsr', SensorState.ERROR, str(e))
                self._safe_emit('device_status', {
                    'device': 'gsr', 'ok': False, 'msg': str(e),
                })
                time.sleep(3)
                continue

            self.registry.set_state(
                'gsr', SensorState.STREAMING,
                f'{port} @ {self.baud}',
            )
            self._safe_emit('device_status', {
                'device': 'gsr', 'ok': True,
                'msg': f'{port} @ {self.baud}',
            })
            ser.reset_input_buffer()

            line_q = _queue.Queue(maxsize=520)  # ~10 s at 52 Hz; drop stale lines on overflow
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
                                        pass  # drop stale GSR lines; real-time stream
                        else:
                            time.sleep(0.001)
                    except Exception:
                        break

            reader_thread = threading.Thread(target=reader_thread_fn, daemon=True, name="gsr-serial-drainer")
            reader_thread.start()

            csv_f = None
            csv_w = None
            local_samples = 0
            csv_flush_counter = 0
            disconnected = False

            try:
                while not self._stop.is_set():
                    try:
                        line = line_q.get(timeout=0.1)
                    except _queue.Empty:
                        continue

                    try:
                        data = json.loads(line)
                        uS = data.get('uS', 0)
                        raw = data.get('raw', 0)
                        stress = data.get('stress', 0)
                        zscore = data.get('zScore', 0)
                        cal_progress = data.get('cal_progress')  # Present during calibration
                    except Exception:
                        continue

                    local_samples += 1
                    self.registry.set_counter('gsr', local_samples)

                    # Emit calibration data on every sample, live data every 2nd
                    is_calibrating = (stress == -1)
                    if is_calibrating or local_samples % 2 == 0:
                        payload = {
                            'uS': uS, 'raw': raw, 'stress': stress,
                            'zscore': zscore, 'n': local_samples,
                        }
                        if cal_progress is not None:
                            payload['cal_progress'] = cal_progress
                        self._safe_emit('gsr_data', payload)

                    # Recording
                    if self.recording and self.session_dir:
                        if csv_f is None:
                            p = os.path.join(
                                self.session_dir, 'gsr', 'gsr_log.csv',
                            )
                            csv_f = open(p, 'a', newline='', buffering=1)
                            csv_w = csv.writer(csv_f)
                            if csv_f.tell() == 0:
                                csv_w.writerow([
                                    'timestamp_s', 'uS', 'raw',
                                    'stress', 'zscore',
                                ])
                        t = time.monotonic() - self.rec_start
                        csv_w.writerow([f'{t:.4f}', uS, raw, stress, zscore])
                        csv_flush_counter += 1
                        if csv_flush_counter >= 260:
                            csv_f.flush()
                            csv_flush_counter = 0
                    elif csv_f is not None:
                        csv_f.close()
                        csv_f = None
                        csv_flush_counter = 0

            except Exception as e:
                disconnected = True
                self._safe_emit('device_status', {
                    'device': 'gsr', 'ok': False,
                    'msg': f'Error: {str(e)[:80]}',
                })
            finally:
                stop_reader.set()
                try:
                    reader_thread.join(timeout=1.0)
                except Exception:
                    pass
                try:
                    ser.close()
                except Exception:
                    pass
                if csv_f:
                    csv_f.close()
                self.registry.set_state('gsr', SensorState.DISCONNECTED)
                self.registry.release_port('gsr')

            if disconnected and not self._stop.is_set():
                self._safe_emit('device_status', {
                    'device': 'gsr', 'ok': False,
                    'msg': 'Reconnecting in 3s...',
                })
                for _ in range(30):
                    if self._stop.is_set():
                        return
                    time.sleep(0.1)
                continue
            elif self._stop.is_set():
                break
            else:
                for _ in range(50):
                    if self._stop.is_set():
                        return
                    time.sleep(0.1)
                continue

    def stop(self):
        """Signal the monitoring loop to stop."""
        self._stop.set()
