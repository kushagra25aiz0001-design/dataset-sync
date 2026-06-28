"""
Sensor Orchestrator — Startup Discovery & Lifecycle Management
==============================================================
Handles the critical startup phase where all serial ports are probed
sequentially to identify which sensor is connected to which port.
This eliminates the race conditions that occurred when each sensor
loop independently tried to claim ports.

Discovery Protocol:
    1. Enumerate all /dev/ttyUSB* and /dev/ttyACM* ports
    2. For user-assigned ports, validate and reserve them
    3. For auto-detect ports, probe each unassigned port:
       a. Send WHORU handshake → check for known identity response
       b. If no WHORU response, try protocol-specific detection
    4. Return a mapping of sensor_name → assigned port

Sensor Identity Responses (WHORU protocol):
    - CSI receiver ESP32:  "CSI_RX"
    - EMG ESP32-S3:        "EMG_S3"
    - GSR ESP32:           "GSR_ESP"
    - Oximeter (CP210x):   No WHORU — detected via VID/PID + data pattern
"""

import time
import logging
import threading
from typing import Optional, Dict, List, Tuple

import serial
import serial.tools.list_ports

from src.recorder.sensor_registry import (
    SensorRegistry, SensorState, ALL_SENSOR_NAMES
)

logger = logging.getLogger(__name__)


# ─── Constants ───────────────────────────────────────────────────

# Known USB identifiers for specific sensors
OXIMETER_VIDS = [0x10C4]       # Silicon Labs (Contec CMS50E)
OXIMETER_PIDS = [0xEA60]       # CP210x

# WHORU handshake settings
WHORU_COMMAND = b"WHORU\n"
WHORU_TIMEOUT = 1.5            # seconds to wait for response
WHORU_IDENTITIES = {
    "CSI_RX":  "csi",
    "EMG_S3":  "emg",
    "GSR_ESP": "gsr",
}

OXIMETER_SERIAL_CONFIGS = [
    # Fixed to user request: 115200 8N1 only
    (115200, serial.PARITY_NONE),
]

PARITY_LABELS = {
    serial.PARITY_NONE: 'N',
    serial.PARITY_EVEN: 'E',
    serial.PARITY_ODD:  'O',
}

# Oximeter trigger commands
OXIMETER_TRIGGERS = [
    bytes([0xA7]),
    bytes([0x7D, 0x81, 0xA7, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80]),
    bytes([0x7D, 0x81, 0xA1, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80]),
    b'\x01', b'\xff', b'\x80',
]


class SensorOrchestrator:
    """
    Manages the sensor discovery and lifecycle.

    The orchestrator owns the startup sequence: it probes ports,
    identifies sensors, assigns ports, and then launches sensor
    monitoring threads. It also provides graceful shutdown.
    """

    def __init__(self, registry: SensorRegistry, sio=None):
        self.registry = registry
        self.sio = sio
        self._stop = threading.Event()

    def _emit(self, event: str, data: dict) -> None:
        """Emit a SocketIO event if sio is available."""
        if self.sio:
            self.sio.emit(event, data)

    def _status(self, sensor: str, ok: bool, msg: str) -> None:
        """Update registry state and emit status to UI."""
        state = SensorState.CONNECTED if ok else SensorState.SCANNING
        self.registry.set_state(sensor, state, msg)
        self._emit('device_status', {'device': sensor, 'ok': ok, 'msg': msg})

    # ── Port Enumeration ─────────────────────────────────────────

    @staticmethod
    def list_serial_ports() -> List[dict]:
        """
        List all available serial ports with metadata.
        Returns list of dicts with: device, vid, pid, description, serial_number.
        """
        result = []
        for port in serial.tools.list_ports.comports():
            if 'ttyUSB' in port.device or 'ttyACM' in port.device:
                result.append({
                    'device': port.device,
                    'vid': port.vid,
                    'pid': port.pid,
                    'description': port.description or '',
                    'serial_number': port.serial_number or '',
                    'manufacturer': port.manufacturer or '',
                })
        return sorted(result, key=lambda p: p['device'])

    # ── WHORU Handshake ──────────────────────────────────────────

    def _probe_whoru(self, port: str, baud: int = 115200) -> Optional[str]:
        """
        Send WHORU command and check for a known identity response.

        Returns the sensor name ("csi", "emg", "gsr") or None.
        This is the fastest and most reliable identification method
        for ESP32-based sensors that support the handshake.
        """
        try:
            ser = serial.Serial(
                port=port, baudrate=baud, timeout=WHORU_TIMEOUT
            )
            ser.reset_input_buffer()

            # Send the handshake
            ser.write(WHORU_COMMAND)
            time.sleep(0.1)

            # Read response (may contain debug output, so scan for identity)
            response = b""
            deadline = time.monotonic() + WHORU_TIMEOUT
            while time.monotonic() < deadline:
                chunk = ser.read(ser.in_waiting or 1)
                if chunk:
                    response += chunk
                    if len(response) > 4096:
                        response = response[-4096:]  # keep tail; identity string is always short
                    # Check if any known identity appears in the response
                    decoded = response.decode('utf-8', errors='ignore')
                    for identity, sensor_name in WHORU_IDENTITIES.items():
                        if identity in decoded:
                            ser.close()
                            logger.info(
                                f"WHORU on {port}: identified as {sensor_name} "
                                f"({identity})"
                            )
                            return sensor_name
                if not chunk:
                    time.sleep(0.05)

            ser.close()
        except serial.SerialException:
            pass
        return None

    # ── Oximeter Detection ───────────────────────────────────────

    @staticmethod
    def _is_oximeter_by_vid_pid(port_info: dict) -> bool:
        """Check if a port has the Silicon Labs CP210x VID/PID."""
        return (
            port_info.get('vid') in OXIMETER_VIDS and
            port_info.get('pid') in OXIMETER_PIDS
        )

    def _probe_oximeter_passive(self, port: str, baud: int,
                                 parity, timeout: float = 2.0) -> bool:
        """
        Passive oximeter detection: just listen for high-bit data.
        The CMS50E auto-streams when in Upload mode.
        """
        try:
            ser = serial.Serial(
                port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE, parity=parity, timeout=timeout
            )
            ser.reset_input_buffer()
            chunk = ser.read(20)
            ser.close()
            if len(chunk) > 5 and any(b >= 128 for b in chunk):
                return True
        except serial.SerialException:
            pass
        return False

    def _probe_oximeter_triggered(self, port: str, baud: int,
                                   parity) -> bool:
        """
        Active oximeter detection: send trigger commands, then listen.
        Needed for models that require a wake command (CMS50D+).
        """
        try:
            ser = serial.Serial(
                port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE, parity=parity, timeout=1.5
            )
            ser.reset_input_buffer()
            for trig in OXIMETER_TRIGGERS:
                try:
                    ser.write(trig)
                    time.sleep(0.03)
                except Exception:
                    pass
            time.sleep(0.3)
            chunk = ser.read(20)
            ser.close()
            if len(chunk) > 5 and any(b >= 128 for b in chunk):
                return True
        except serial.SerialException:
            pass
        return False

    def _detect_oximeter(self, port: str) -> Tuple[Optional[int], Optional[int]]:
        """
        Two-phase oximeter detection on a specific port.
        Returns (baud, parity) or (None, None).
        """
        # Phase 1: Passive listen (fast)
        for baud, parity in OXIMETER_SERIAL_CONFIGS:
            if self._stop.is_set():
                return None, None
            if self._probe_oximeter_passive(port, baud, parity):
                return baud, parity

        # Phase 2: Triggered (slower, for devices needing wake)
        for baud, parity in OXIMETER_SERIAL_CONFIGS:
            if self._stop.is_set():
                return None, None
            if self._probe_oximeter_triggered(port, baud, parity):
                return baud, parity

        return None, None

    # ── Protocol-Specific Detection (Fallback) ───────────────────

    def _probe_emg_binary(self, port: str, baud: int = 230400) -> bool:
        """
        Detect EMG sensor by looking for the binary sync pattern
        (0xC7 0x7C) in the data stream.
        """
        try:
            ser = serial.Serial(port=port, baudrate=baud, timeout=1.5)
            ser.reset_input_buffer()
            time.sleep(0.3)
            data = ser.read(min(ser.in_waiting, 256) or 72)
            ser.close()
            # Look for the EMG sync bytes
            for i in range(len(data) - 1):
                if data[i] == 0xC7 and data[i + 1] == 0x7C:
                    return True
        except serial.SerialException:
            pass
        return False

    def _probe_gsr_json(self, port: str, baud: int = 115200) -> bool:
        """
        Detect GSR sensor by looking for JSON lines with expected keys
        (uS, raw, stress).
        """
        try:
            ser = serial.Serial(port=port, baudrate=baud, timeout=2.0)
            ser.reset_input_buffer()
            time.sleep(0.3)
            for _ in range(5):
                line = ser.readline()
                if not line:
                    continue
                decoded = line.decode('utf-8', errors='ignore').strip()
                if '"uS"' in decoded and '"raw"' in decoded:
                    ser.close()
                    return True
            ser.close()
        except serial.SerialException:
            pass
        return False

    def _probe_csi_csv(self, port: str, baud: int = 115200) -> bool:
        """
        Detect CSI receiver by looking for CSV data lines
        (starts with digit, many comma-separated values).
        """
        try:
            ser = serial.Serial(port=port, baudrate=baud, timeout=2.0)
            ser.reset_input_buffer()
            time.sleep(0.3)
            for _ in range(10):
                line = ser.readline()
                if not line:
                    continue
                decoded = line.decode('utf-8', errors='ignore').strip()
                if decoded and decoded[0].isdigit() and decoded.count(',') > 10:
                    ser.close()
                    return True
            ser.close()
        except serial.SerialException:
            pass
        return False

    # ── Main Discovery ───────────────────────────────────────────

    def discover_all(self, port_assignments: Dict[str, str],
                     baud_rates: Optional[Dict[str, int]] = None) -> dict:
        """
        Main discovery entry point. Called at startup.

        Args:
            port_assignments: User-specified mapping from the setup modal.
                e.g. {"camera": "/dev/video0", "oximeter": "/dev/ttyUSB0",
                       "emg": "auto", "gsr": "none"}
            baud_rates: Optional baud rate overrides per sensor.

        Returns:
            dict of sensor_name → assigned port (or None if disabled/not found).
        """
        if baud_rates is None:
            baud_rates = {}

        result = {}
        available_ports = self.list_serial_ports()
        available_port_set = {p['device'] for p in available_ports}

        logger.info(f"Discovery: {len(available_ports)} serial ports available")
        for p in available_ports:
            vid_str = f"VID={p['vid']:04X}" if p['vid'] else "VID=?"
            pid_str = f"PID={p['pid']:04X}" if p['pid'] else "PID=?"
            logger.info(f"  {p['device']}: {p['description']} ({vid_str} {pid_str})")

        # ── Step 1: Handle explicit assignments (user chose a specific port)
        for sensor in ALL_SENSOR_NAMES:
            assignment = port_assignments.get(sensor, 'auto')

            if assignment == 'none':
                self.registry.set_state(sensor, SensorState.DISABLED, "Disabled")
                result[sensor] = None
                continue

            if assignment != 'auto' and sensor != 'camera':
                # User assigned a specific serial port
                if assignment in available_port_set:
                    if self.registry.reserve_port(sensor, assignment):
                        baud = baud_rates.get(sensor, 0)
                        self.registry.set_port(sensor, assignment, baud)
                        self.registry.set_state(
                            sensor, SensorState.DETECTED,
                            f"Assigned to {assignment}"
                        )
                        result[sensor] = assignment
                        logger.info(f"  {sensor}: manually assigned to {assignment}")
                    else:
                        owner = self.registry.get_port_owner(assignment)
                        self.registry.set_state(
                            sensor, SensorState.ERROR,
                            f"Port {assignment} already claimed by {owner}"
                        )
                        result[sensor] = None
                else:
                    self.registry.set_state(
                        sensor, SensorState.ERROR,
                        f"Port {assignment} not found"
                    )
                    result[sensor] = None
                continue

            # Camera is handled separately (V4L2, not serial)
            if sensor == 'camera' and assignment != 'none':
                result[sensor] = assignment
                self.registry.set_state(sensor, SensorState.DETECTED,
                                        f"Source: {assignment}")
                continue

        # ── Step 2: Auto-detect unassigned sensors on remaining ports
        auto_sensors = [
            s for s in ALL_SENSOR_NAMES
            if s not in result and port_assignments.get(s) == 'auto'
        ]

        if not auto_sensors:
            return result

        unassigned_ports = [
            p for p in available_ports
            if not self.registry.is_port_reserved(p['device'])
        ]

        logger.info(
            f"Auto-detecting {len(auto_sensors)} sensors on "
            f"{len(unassigned_ports)} unassigned ports"
        )

        for sensor in auto_sensors:
            self._status(sensor, False, "Scanning...")

        # 2a. VID/PID-based detection (oximeter)
        if 'oximeter' in auto_sensors:
            for port_info in unassigned_ports:
                if self._is_oximeter_by_vid_pid(port_info):
                    port = port_info['device']
                    if self.registry.reserve_port('oximeter', port):
                        self.registry.set_state(
                            'oximeter', SensorState.DETECTED,
                            f"Found CP210x at {port}"
                        )
                        result['oximeter'] = port
                        auto_sensors.remove('oximeter')
                        logger.info(f"  oximeter: VID/PID match at {port}")
                        break

        # Refresh unassigned list
        unassigned_ports = [
            p for p in available_ports
            if not self.registry.is_port_reserved(p['device'])
        ]

        # 2b. WHORU handshake on remaining ports
        for port_info in list(unassigned_ports):
            if not auto_sensors:
                break
            if self._stop.is_set():
                break

            port = port_info['device']
            self._emit('device_status', {
                'device': 'system', 'ok': False,
                'msg': f'Probing {port}...'
            })

            sensor_name = self._probe_whoru(port)
            if sensor_name and sensor_name in auto_sensors:
                if self.registry.reserve_port(sensor_name, port):
                    baud = baud_rates.get(sensor_name, 115200)
                    self.registry.set_port(sensor_name, port, baud)
                    self.registry.set_state(
                        sensor_name, SensorState.DETECTED,
                        f"Identified via WHORU at {port}"
                    )
                    result[sensor_name] = port
                    auto_sensors.remove(sensor_name)
                    unassigned_ports.remove(port_info)
                    logger.info(f"  {sensor_name}: WHORU match at {port}")

        # 2c. Protocol-specific fallback detection
        probe_methods = {
            'emg': (self._probe_emg_binary, baud_rates.get('emg', 230400)),
            'gsr': (self._probe_gsr_json, baud_rates.get('gsr', 115200)),
            'csi': (self._probe_csi_csv, baud_rates.get('csi', 115200)),
        }

        for sensor_name in list(auto_sensors):
            if sensor_name not in probe_methods:
                continue
            if self._stop.is_set():
                break

            probe_fn, baud = probe_methods[sensor_name]
            for port_info in unassigned_ports:
                if self.registry.is_port_reserved(port_info['device']):
                    continue
                port = port_info['device']
                if probe_fn(port, baud):
                    if self.registry.reserve_port(sensor_name, port):
                        self.registry.set_port(sensor_name, port, baud)
                        self.registry.set_state(
                            sensor_name, SensorState.DETECTED,
                            f"Detected via data pattern at {port}"
                        )
                        result[sensor_name] = port
                        auto_sensors.remove(sensor_name)
                        logger.info(
                            f"  {sensor_name}: protocol match at {port}"
                        )
                        break

        # 2d. If oximeter wasn't found by VID/PID, try data-pattern detection
        if 'oximeter' in auto_sensors:
            for port_info in available_ports:
                if self.registry.is_port_reserved(port_info['device']):
                    continue
                if self._stop.is_set():
                    break
                port = port_info['device']
                baud, parity = self._detect_oximeter(port)
                if baud is not None:
                    if self.registry.reserve_port('oximeter', port):
                        p_label = PARITY_LABELS.get(parity, 'N')
                        p_str = {serial.PARITY_NONE: 'NONE',
                                 serial.PARITY_EVEN: 'EVEN',
                                 serial.PARITY_ODD: 'ODD'}.get(parity, 'NONE')
                        self.registry.set_port('oximeter', port, baud, p_str)
                        self.registry.set_state(
                            'oximeter', SensorState.DETECTED,
                            f"Detected at {port} @ {baud}/{p_str}"
                        )
                        result['oximeter'] = port
                        auto_sensors.remove('oximeter')
                        logger.info(f"  oximeter: data match at {port}")
                        break

        # Mark any remaining sensors as not found
        for sensor_name in auto_sensors:
            self.registry.set_state(
                sensor_name, SensorState.ERROR,
                "Device not found — check connection"
            )
            result[sensor_name] = None
            logger.warning(f"  {sensor_name}: not found on any port")

        return result

    def stop(self) -> None:
        """Signal all operations to stop."""
        self._stop.set()
