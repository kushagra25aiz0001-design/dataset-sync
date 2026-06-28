"""
Sensor Registry — Thread-Safe Centralized State Management
==========================================================
Provides a single source of truth for all sensor states, port assignments,
and sample counters. All mutations are protected by a threading lock.

This replaces the ad-hoc attribute tracking that was spread across
DeviceManager, where multiple sensor threads mutated shared state
without synchronization.

Usage:
    registry = SensorRegistry()
    registry.set_state("oximeter", SensorState.CONNECTED)
    registry.increment_counter("oximeter", 100)
    snapshot = registry.get_status_snapshot()
"""

import time
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


class SensorState(Enum):
    """Lifecycle states for any sensor device."""
    DISABLED = "disabled"           # User chose not to use this sensor
    SCANNING = "scanning"           # Actively probing for device
    DETECTED = "detected"           # Device found, not yet streaming
    CONNECTED = "connected"         # Serial/video port opened successfully
    STREAMING = "streaming"         # Actively receiving valid data
    ERROR = "error"                 # Recoverable error (will retry)
    DISCONNECTED = "disconnected"   # Was connected, lost connection


@dataclass
class SensorInfo:
    """Mutable state for a single sensor."""
    name: str
    display_name: str
    icon: str
    state: SensorState = SensorState.DISABLED
    port: Optional[str] = None
    baud: int = 0
    parity: str = "NONE"
    protocol: Optional[str] = None
    sample_count: int = 0
    last_seen: float = 0.0
    error_msg: Optional[str] = None
    status_msg: str = "Disabled"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize for JSON API responses."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "icon": self.icon,
            "state": self.state.value,
            "port": self.port,
            "baud": self.baud,
            "protocol": self.protocol,
            "sample_count": self.sample_count,
            "ok": self.state in (SensorState.CONNECTED, SensorState.STREAMING),
            "status_msg": self.status_msg,
            "error_msg": self.error_msg,
        }


# ─── Sensor Definitions ─────────────────────────────────────────

SENSOR_DEFINITIONS = {
    "camera": {
        "display_name": "Camera",
        "icon": "🎥",
    },
    "oximeter": {
        "display_name": "Pulse Oximeter",
        "icon": "💓",
    },
    "csi": {
        "display_name": "WiFi CSI",
        "icon": "📶",
    },
    "emg": {
        "display_name": "Muscle EMG",
        "icon": "⚡",
    },
    "gsr": {
        "display_name": "Skin GSR",
        "icon": "💧",
    },
}

ALL_SENSOR_NAMES = list(SENSOR_DEFINITIONS.keys())


class SensorRegistry:
    """
    Thread-safe registry of all sensor states.

    All state mutations go through this class, which holds a single
    lock protecting the entire state dictionary. This is simple and
    correct — the lock is held for microseconds per operation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sensors: Dict[str, SensorInfo] = {}
        self._port_reservations: Dict[str, str] = {}  # port → sensor_name

        # Initialize all known sensors
        for name, defn in SENSOR_DEFINITIONS.items():
            self._sensors[name] = SensorInfo(
                name=name,
                display_name=defn["display_name"],
                icon=defn["icon"],
            )

    # ── State Mutations ──────────────────────────────────────────

    def set_state(self, sensor: str, state: SensorState,
                  msg: Optional[str] = None) -> None:
        """Update a sensor's state and optional status message."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info is None:
                return
            info.state = state
            if msg is not None:
                info.status_msg = msg
            if state == SensorState.ERROR:
                info.error_msg = msg

    def set_port(self, sensor: str, port: Optional[str],
                 baud: int = 0, parity: str = "NONE") -> None:
        """Assign a port to a sensor."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info is None:
                return
            info.port = port
            info.baud = baud
            info.parity = parity

    def set_protocol(self, sensor: str, protocol: str) -> None:
        """Record the detected protocol for a sensor."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info:
                info.protocol = protocol

    def set_metadata(self, sensor: str, metadata: dict) -> None:
        """Store device-specific metadata (e.g., camera resolution)."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info:
                info.metadata.update(metadata)

    def increment_counter(self, sensor: str, delta: int = 1) -> int:
        """Atomically increment the sample counter. Returns new value."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info is None:
                return 0
            info.sample_count += delta
            info.last_seen = time.monotonic()
            return info.sample_count

    def set_counter(self, sensor: str, value: int) -> None:
        """Set the sample counter to a specific value."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info:
                info.sample_count = value

    def reset_counter(self, sensor: str) -> None:
        """Reset the sample counter to zero."""
        self.set_counter(sensor, 0)

    def reset_all_counters(self) -> None:
        """Reset all sample counters (called at recording start)."""
        with self._lock:
            for info in self._sensors.values():
                info.sample_count = 0

    # ── Port Reservation ─────────────────────────────────────────

    def reserve_port(self, sensor: str, port: str) -> bool:
        """
        Reserve a serial port for exclusive use by a sensor.
        Returns True if reservation succeeded, False if port is
        already reserved by another sensor.
        """
        with self._lock:
            existing = self._port_reservations.get(port)
            if existing is not None and existing != sensor:
                return False
            self._port_reservations[port] = sensor
            info = self._sensors.get(sensor)
            if info:
                info.port = port
            return True

    def release_port(self, sensor: str) -> None:
        """Release the port reserved by this sensor."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info and info.port:
                self._port_reservations.pop(info.port, None)

    def is_port_reserved(self, port: str) -> bool:
        """Check if a port is reserved by any sensor."""
        with self._lock:
            return port in self._port_reservations

    def get_port_owner(self, port: str) -> Optional[str]:
        """Get the sensor name that owns a port, or None."""
        with self._lock:
            return self._port_reservations.get(port)

    # ── Queries ──────────────────────────────────────────────────

    def is_ok(self, sensor: str) -> bool:
        """Check if a sensor is in a healthy state."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info is None:
                return False
            return info.state in (SensorState.CONNECTED, SensorState.STREAMING)

    def get_counter(self, sensor: str) -> int:
        """Get the current sample count for a sensor."""
        with self._lock:
            info = self._sensors.get(sensor)
            return info.sample_count if info else 0

    def get_sensor(self, sensor: str) -> Optional[SensorInfo]:
        """Get a copy of a sensor's info (for read-only use)."""
        with self._lock:
            info = self._sensors.get(sensor)
            if info is None:
                return None
            # Return a shallow copy to prevent unsynchronized mutations
            import copy
            return copy.copy(info)

    def get_status_snapshot(self) -> dict:
        """
        Get a complete status snapshot for the /api/status endpoint.
        Returns a dict ready for JSON serialization.
        """
        with self._lock:
            return {
                "sensors": {
                    name: info.to_dict()
                    for name, info in self._sensors.items()
                },
                # Legacy flat keys for backward compatibility
                "cam_ok": self._sensors["camera"].state in (
                    SensorState.CONNECTED, SensorState.STREAMING),
                "oxi_ok": self._sensors["oximeter"].state in (
                    SensorState.CONNECTED, SensorState.STREAMING),
                "csi_ok": self._sensors["csi"].state in (
                    SensorState.CONNECTED, SensorState.STREAMING),
                "emg_ok": self._sensors["emg"].state in (
                    SensorState.CONNECTED, SensorState.STREAMING),
                "gsr_ok": self._sensors["gsr"].state in (
                    SensorState.CONNECTED, SensorState.STREAMING),
                "cam_frames": self._sensors["camera"].sample_count,
                "oxi_samples": self._sensors["oximeter"].sample_count,
                "csi_packets": self._sensors["csi"].sample_count,
                "emg_packets": self._sensors["emg"].sample_count,
                "gsr_samples": self._sensors["gsr"].sample_count,
            }
