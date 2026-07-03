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

# Name fragments of HDMI capture cards that carry the camera's embedded audio.
# When the Sony a6000 feeds an HDMI capture card, its audio rides the same stream,
# so capturing THIS device puts audio on the same hardware clock as the video —
# the tightest possible A/V sync.
CAPTURE_CARD_HINTS = ('macrosilicon', 'ms2109', 'ms2130', 'usb video',
                      'usb3. 0', 'usb3.0', 'cam link', 'camlink', 'elgato',
                      'avermedia', 'magewell', 'hdmi', 'capture')


def list_input_devices():
    """[(index, name, max_input_channels)] for every input-capable device."""
    import sounddevice as sd
    return [(i, d['name'], d['max_input_channels'])
            for i, d in enumerate(sd.query_devices())
            if d['max_input_channels'] > 0]


def find_capture_card_device(hints=CAPTURE_CARD_HINTS):
    """Index of the first input device whose name looks like an HDMI capture card
    (i.e. the a6000's audio-over-HDMI), or None if none match."""
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0 and any(h in d['name'].lower() for h in hints):
            return i
    return None


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
        """Open the input stream. Raises ImportError if sounddevice is missing.
        `device` may be an index, a name substring, or the sentinel 'hdmi'/'auto-hdmi'
        to auto-select the HDMI capture card (camera audio-over-HDMI)."""
        import sounddevice as sd                # lazy: audio stack optional
        os.makedirs(self.out_dir, exist_ok=True)

        if isinstance(self.device, str) and self.device.lower() in (
                'hdmi', 'auto-hdmi', 'capture', 'camera'):
            found = find_capture_card_device()
            if found is None:
                raise RuntimeError('no HDMI capture-card audio input found '
                                   '(is the a6000 passing audio over HDMI?)')
            self.device = found

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
            'device': self.device,          # capture-card index → same-clock A/V
            'n_frames': n_frames,
            'duration_s': round(n_frames / self.samplerate, 3) if self.samplerate else 0,
        }
        with open(os.path.join(self.out_dir, 'audio_meta.json'), 'w') as f:
            json.dump(meta, f, indent=2)
        return meta
