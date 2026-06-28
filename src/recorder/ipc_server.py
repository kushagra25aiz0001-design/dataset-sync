"""
IPC Server (Inter-Process Communication)
========================================
A lightweight state broadcaster that replaces Flask-SocketIO in headless mode.
It continuously dumps the SensorRegistry snapshot to a temporary JSON file
so that `viewer_app.py` can read it without locking any serial ports.
"""

import json
import os
import tempfile
import threading
import time

IPC_FILE = os.path.join(tempfile.gettempdir(), 'dataset_sync_ipc.json')

class IpcSIO:
    """
    Fake SocketIO object that silently drops events to save RAM, but
    runs a background thread to dump the registry snapshot to disk.
    """
    def __init__(self, registry):
        self.registry = registry
        self._stop = threading.Event()
        self._thread = None
        self._recording = False
        self._session_id = None
        self._duration = 0
        self._rec_start = None
        self._latest_oxi = None
        self._latest_emg = None
        self._latest_csi = None
        self._latest_gsr = None

    def emit(self, event, data=None, **kwargs):
        """Silently discard standard socket events, but cache latest data."""
        if event == 'oxi_data':
            self._latest_oxi = data
        elif event == 'emg_data':
            self._latest_emg = data
        elif event == 'csi_data':
            self._latest_csi = data
        elif event == 'gsr_data':
            self._latest_gsr = data

    def set_recording_state(self, is_recording, session_id=None, duration=0, rec_start=None):
        self._recording = is_recording
        self._session_id = session_id
        self._duration = duration
        self._rec_start = rec_start

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._dump_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            
    def _dump_loop(self):
        """Dumps state to JSON file at 2 Hz."""
        while not self._stop.is_set():
            snapshot = self.registry.get_status_snapshot()
            
            elapsed = 0
            if self._recording and self._rec_start:
                elapsed = round(time.monotonic() - self._rec_start, 1)
                
            snapshot.update({
                'recording': self._recording,
                'session': self._session_id,
                'elapsed': elapsed,
                'duration': self._duration,
                'latest_oxi': self._latest_oxi,
                'latest_emg': self._latest_emg,
                'latest_csi': self._latest_csi,
                'latest_gsr': self._latest_gsr,
            })
            
            # Atomic write
            tmp = IPC_FILE + '.tmp'
            try:
                with open(tmp, 'w') as f:
                    json.dump(snapshot, f)
                os.rename(tmp, IPC_FILE)
            except Exception:
                pass
                
            time.sleep(0.5)
