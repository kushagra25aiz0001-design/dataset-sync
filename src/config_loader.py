"""
Configuration Loader
====================
Loads and validates recording_config.yaml, providing typed access
to all configuration values with sensible defaults.

Usage:
    from src.config_loader import load_config
    cfg = load_config()
    print(cfg['camera']['resolution'])   # [1920, 1080]
    print(cfg['emg']['baud_rate'])       # 230400
"""

import os
from typing import Any, Dict

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


# ─── Defaults ────────────────────────────────────────────────────

DEFAULT_CONFIG: Dict[str, Any] = {
    "session": {
        "output_dir": "data/raw",
        "naming_format": "session_{timestamp}",
        "default_duration_seconds": 60,
    },
    "camera": {
        "device_id": 0,
        "resolution": [1920, 1080],
        "fps": 30,
        "codec": "MJPG",
        "save_format": "video",
        "frame_quality": 95,
    },
    "oximeter": {
        "port": "auto",
        "baud_rate": 9600,
        "parity": "none",
        "timeout_seconds": 1.0,
        "sample_rate_hz": 60,
    },
    "csi": {
        "port": "/dev/ttyUSB1",
        "baud_rate": 115200,
        "channel": 6,
        "bandwidth": "HT20",
        "packet_rate_hz": 100,
        "num_subcarriers": 52,
        "timeout_seconds": 1.0,
    },
    "emg": {
        "port": "auto",
        "baud_rate": 230400,
        "channels": 16,
        "sample_rate_hz": 250,
        "packet_size_bytes": 36,
        "sync_bytes": [0xC7, 0x7C],
        "timeout_seconds": 1.0,
    },
    "gsr": {
        "port": "auto",
        "baud_rate": 115200,
        "sample_rate_hz": 10,
        "data_format": "json",
        "timeout_seconds": 1.0,
    },
    "orchestrator": {
        "discovery_timeout_s": 30,
        "whoru_timeout_s": 1.5,
        "max_retries": 5,
        "retry_delay_s": 8,
    },
    "sync": {
        "clock_source": "monotonic",
        "pre_recording_countdown_seconds": 3,
        "log_interval_seconds": 5,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = None) -> Dict[str, Any]:
    """
    Load configuration from YAML file, merged with defaults.

    Args:
        config_path: Path to YAML config file. If None, searches for
                     config/recording_config.yaml relative to project root.

    Returns:
        Complete configuration dict with all defaults filled in.
    """
    if config_path is None:
        # Search relative to this file's location
        project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..')
        )
        config_path = os.path.join(project_root, 'config', 'recording_config.yaml')

    config = DEFAULT_CONFIG.copy()

    if yaml is None:
        # PyYAML not installed — use defaults silently
        return config

    if not os.path.exists(config_path):
        return config

    try:
        with open(config_path, 'r') as f:
            user_config = yaml.safe_load(f) or {}
        config = _deep_merge(DEFAULT_CONFIG, user_config)
    except Exception as e:
        print(f"[CONFIG] Warning: Failed to load {config_path}: {e}")
        print("[CONFIG] Using default configuration.")

    return config
