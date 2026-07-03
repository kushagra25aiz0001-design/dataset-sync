"""
ECG Recorder — Polar H10 (BLE) ECG on the master clock
======================================================
The ECG ground truth is a Polar H10 chest strap streaming raw ECG over Bluetooth
LE via Polar's **PMD** (Polar Measurement Data) service at 130 Hz. This module
connects to it, streams ECG, and writes each sample on the recorder's master clock
so ECG lines up with the camera / rPPG / oximeter.

Clock model (same idea as the audio path)
-----------------------------------------
BLE delivers ECG in *batches* with connection-interval jitter, so we do NOT stamp
each sample with its noisy arrival time. Instead we anchor the **first** sample to
the master clock (``monotonic() - t0`` at the first notification) and place sample
*k* at ``t_start_master + k / samplerate`` (130 Hz nominal). The Polar per-frame
device timestamp (nanoseconds) is stored alongside every sample so the true rate
can be refit offline if needed (the sensor's oscillator is far steadier than BLE
delivery). This mirrors how ``audio_recorder`` anchors a continuous stream.

Output (under <session_dir>/ecg/):
    ecg_log.csv    timestamp_s, ecg_uV, device_ts_ns

`bleak`/`asyncio` are imported lazily inside start(), so this module (and the PMD
parser below) import with no BLE stack installed — the parser is unit-tested
without hardware.
"""

import csv
import os
import threading
import time
from typing import List, Optional, Tuple

# Polar Measurement Data (PMD) GATT service.
PMD_SERVICE = 'fb005c80-02e7-f387-1cad-8acd2d8df0c8'
PMD_CONTROL = 'fb005c81-02e7-f387-1cad-8acd2d8df0c8'
PMD_DATA = 'fb005c82-02e7-f387-1cad-8acd2d8df0c8'

# Start ECG stream: measurement type 0x00 (ECG), 130 Hz, 14-bit resolution.
ECG_START_CMD = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00,
                           0x01, 0x01, 0x0E, 0x00])
ECG_SAMPLE_HZ = 130


def parse_ecg_frame(data: bytes) -> Optional[Tuple[int, int, List[int]]]:
    """Parse one PMD ECG notification into (device_ts_ns, frame_type, samples_uV).

    Layout:
        byte 0     measurement type (0x00 = ECG)
        byte 1..8  uint64 LE timestamp of the last sample (nanoseconds)
        byte 9     frame type (0x00 = int24 raw samples)
        byte 10..  samples, 3 bytes each, signed little-endian int24, microvolts

    Returns None for a non-ECG / malformed frame. Pure function (unit-tested).
    """
    if len(data) < 10 or data[0] != 0x00:
        return None
    device_ts_ns = int.from_bytes(data[1:9], 'little', signed=False)
    frame_type = data[9]
    body = data[10:]
    samples: List[int] = []
    for i in range(0, len(body) - 2, 3):
        samples.append(int.from_bytes(body[i:i + 3], 'little', signed=True))
    return device_ts_ns, frame_type, samples


class PolarH10ECGRecorder:
    def __init__(self, out_dir: str, t0: float, address: Optional[str] = None,
                 samplerate: int = ECG_SAMPLE_HZ, name_hint: str = 'Polar'):
        self.out_dir = out_dir
        self.t0 = float(t0)
        self.address = address           # BLE MAC/UUID; None → scan by name
        self.samplerate = int(samplerate)
        self.name_hint = name_hint
        self._t_start_master: Optional[float] = None
        self._n_samples = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._csv_f = None
        self._csv_w = None
        self._lock = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        """Spin up a background thread running the BLE event loop. Raises
        ImportError if bleak is missing (caller degrades gracefully)."""
        import bleak  # noqa: F401  — fail fast if the BLE stack is absent
        os.makedirs(self.out_dir, exist_ok=True)
        self._csv_f = open(os.path.join(self.out_dir, 'ecg_log.csv'),
                           'a', newline='', buffering=1)
        self._csv_w = csv.writer(self._csv_f)
        if self._csv_f.tell() == 0:
            self._csv_w.writerow(['timestamp_s', 'ecg_uV', 'device_ts_ns'])
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name='ecg-ble')
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            if self._csv_f is not None:
                self._csv_f.flush()
                self._csv_f.close()
                self._csv_f = None
        return {'n_samples': self._n_samples,
                't_start_master_s': self._t_start_master}

    # ── data path ─────────────────────────────────────────────────────────────

    def _on_ecg(self, _sender, data: bytearray):
        parsed = parse_ecg_frame(bytes(data))
        if parsed is None:
            return
        device_ts_ns, _ftype, samples = parsed
        if not samples:
            return
        if self._t_start_master is None:
            # Anchor sample 0 to the master clock at first receipt. Back-date so
            # the whole first batch sits just before "now".
            self._t_start_master = (time.monotonic() - self.t0
                                    - (len(samples) - 1) / self.samplerate)
        with self._lock:
            if self._csv_w is None:
                return
            for v in samples:
                t = self._t_start_master + self._n_samples / self.samplerate
                self._csv_w.writerow([f'{t:.5f}', v, device_ts_ns])
                self._n_samples += 1

    def _run_loop(self):
        import asyncio
        try:
            asyncio.run(self._ble_main())
        except Exception as e:
            print(f'  ⚠ ECG BLE loop ended: {str(e)[:120]}')

    async def _ble_main(self):
        from bleak import BleakClient, BleakScanner

        address = self.address
        if address is None:
            dev = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or '').startswith(self.name_hint), timeout=10.0)
            if dev is None:
                raise RuntimeError(f'no BLE device named {self.name_hint!r} found')
            address = dev.address

        async with BleakClient(address) as client:
            await client.start_notify(PMD_DATA, self._on_ecg)
            await client.write_gatt_char(PMD_CONTROL, ECG_START_CMD, response=True)
            while not self._stop.is_set():
                import asyncio
                await asyncio.sleep(0.1)
            try:
                await client.stop_notify(PMD_DATA)
            except Exception:
                pass
