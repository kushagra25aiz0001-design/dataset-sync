"""
Audio Recorder — recorder-owned microphone stream on the master clock
=====================================================================
Tier-B tasks (arithmetic, verbal fluency, passage reading, picture description)
depend on speech. That audio MUST be a recorder-owned stream on the master clock
— not the iPad/participant-device microphone, which would be a separate, unsynced
device. This module captures the default input device and anchors the first
audio sample to the recorder's master clock (`monotonic() - t0`), so the offline
synchronizer can place the waveform on the shared timeline.

Output (under <session_dir>/audio/):
    audio.wav          16-bit PCM
    audio_meta.json    {t_start_master_s, samplerate, channels, n_frames}

`sounddevice` is imported lazily inside start(), so this module loads (and its
consumers import) without the audio stack installed. There is no mic in the dev
sandbox, so the capture path itself must be validated on real hardware; the
master-clock anchoring and file writing are straightforward.
"""

import json
import os
import threading
import time
import wave
from typing import Optional


class AudioRecorder:
    def __init__(self, out_dir: str, t0: float, samplerate: int = 16000,
                 channels: int = 1, device=None):
        self.out_dir = out_dir
        self.t0 = float(t0)
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.device = device
        self._stream = None
        self._frames = []                       # list of int16 byte chunks
        self._t_start_master: Optional[float] = None
        self._lock = threading.Lock()

    def start(self):
        """Open the input stream. Raises ImportError if sounddevice is missing."""
        import sounddevice as sd                # lazy: audio stack optional
        os.makedirs(self.out_dir, exist_ok=True)

        def _cb(indata, frames, time_info, status):
            # Anchor the stream to the master clock at the first callback.
            if self._t_start_master is None:
                self._t_start_master = time.monotonic() - self.t0
            with self._lock:
                self._frames.append(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=self.samplerate, channels=self.channels,
            dtype='int16', callback=_cb, device=self.device,
        )
        self._stream.start()

    def stop(self) -> dict:
        """Stop capture, write audio.wav + audio_meta.json, return the metadata."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None

        os.makedirs(self.out_dir, exist_ok=True)
        with self._lock:
            data = b''.join(self._frames)
        wav_path = os.path.join(self.out_dir, 'audio.wav')
        with wave.open(wav_path, 'wb') as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(2)                  # int16
            wf.setframerate(self.samplerate)
            wf.writeframes(data)

        n_frames = len(data) // (2 * self.channels)
        meta = {
            't_start_master_s': self._t_start_master,
            'samplerate': self.samplerate,
            'channels': self.channels,
            'n_frames': n_frames,
            'duration_s': round(n_frames / self.samplerate, 3) if self.samplerate else 0,
        }
        with open(os.path.join(self.out_dir, 'audio_meta.json'), 'w') as f:
            json.dump(meta, f, indent=2)
        return meta
