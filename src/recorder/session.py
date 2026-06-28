"""
Session Manager
===============
Handles session directory creation, naming conventions, and metadata
for multi-modal recording sessions.

Supports all 5 modalities: camera, oximeter, CSI, EMG, GSR.
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta


class Session:
    """Manages a single recording session's directory and metadata."""

    # All supported modalities
    MODALITIES = ['camera', 'oximeter', 'csi', 'emg', 'gsr']

    def __init__(self, output_dir: str = "data/raw", subject_id: str = "unknown"):
        self.subject_id = subject_id
        self.start_wall = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        self.start_utc = datetime.now(timezone.utc)
        self.t0 = time.monotonic()

        # Create session directory
        ts = self.start_wall.strftime("%Y%m%d_%H%M%S")
        self.session_id = f"session_{ts}"
        self.session_dir = os.path.join(output_dir, self.session_id)

        # Create subdirectories for all modalities
        self.camera_dir = os.path.join(self.session_dir, "camera")
        self.oximeter_dir = os.path.join(self.session_dir, "oximeter")
        self.csi_dir = os.path.join(self.session_dir, "csi")
        self.emg_dir = os.path.join(self.session_dir, "emg")
        self.gsr_dir = os.path.join(self.session_dir, "gsr")

        for d in [self.camera_dir, self.oximeter_dir, self.csi_dir,
                  self.emg_dir, self.gsr_dir]:
            os.makedirs(d, exist_ok=True)

        self.metadata = {
            "session_id": self.session_id,
            "start_time_utc": self.start_utc.isoformat(),
            "start_time_local": self.start_wall.isoformat(),
            "subject_id": self.subject_id,
            "t0_monotonic": self.t0,
            "duration_seconds": None,
            "devices": {
                modality: {} for modality in self.MODALITIES
            },
            "environment": {
                "location": "indoor",
                "lighting": "unknown",
                "distance_m": None,
                "notes": ""
            },
            "stats": {
                "camera_frames": 0,
                "oximeter_samples": 0,
                "csi_packets": 0,
                "emg_packets": 0,
                "gsr_samples": 0,
            }
        }

    def elapsed(self) -> float:
        """Seconds since session start (monotonic)."""
        return time.monotonic() - self.t0

    def update_stats(self, camera_frames: int = 0, oximeter_samples: int = 0,
                     csi_packets: int = 0, emg_packets: int = 0,
                     gsr_samples: int = 0):
        """Update recording statistics for all modalities."""
        self.metadata["stats"]["camera_frames"] = camera_frames
        self.metadata["stats"]["oximeter_samples"] = oximeter_samples
        self.metadata["stats"]["csi_packets"] = csi_packets
        self.metadata["stats"]["emg_packets"] = emg_packets
        self.metadata["stats"]["gsr_samples"] = gsr_samples

    def update_device_config(self, device: str, config: dict):
        """Store device configuration in metadata."""
        if device in self.MODALITIES:
            self.metadata["devices"][device] = config
        else:
            # Allow arbitrary device names for forward compatibility
            self.metadata["devices"][device] = config

    def finalize(self, duration: float):
        """Write metadata.json after recording ends."""
        self.metadata["duration_seconds"] = round(duration, 2)
        meta_path = os.path.join(self.session_dir, "metadata.json")
        with open(meta_path, "w") as f:
            json.dump(self.metadata, f, indent=2, default=str)
        return meta_path

    def __repr__(self):
        return f"Session({self.session_id}, subject={self.subject_id})"
