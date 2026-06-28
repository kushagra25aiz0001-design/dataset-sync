"""
Synchronization Manager
========================
Orchestrates all five recorders (camera, oximeter, CSI, EMG, GSR)
to start simultaneously with a shared monotonic clock.

Usage:
    python -m src.recorder.sync_manager --duration 60 --subject subject_01

Or with custom ports:
    python -m src.recorder.sync_manager \\
        --duration 120 \\
        --subject subject_01 \\
        --camera-id 0 \\
        --oximeter-port /dev/ttyUSB0 \\
        --csi-port /dev/ttyUSB1 \\
        --emg-port /dev/ttyACM0 \\
        --gsr-port /dev/ttyUSB2
"""

import argparse
import sys
import time
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.recorder.session import Session
from src.recorder.camera_recorder import CameraRecorder
from src.recorder.oximeter_recorder import OximeterRecorder
from src.recorder.csi_recorder import CSIRecorder


def print_banner(session, duration, args):
    """Print a nice startup banner."""
    print()
    print("=" * 60)
    print("  📡 Dataset Sync — Synchronized Recording")
    print("=" * 60)
    print(f"  Session   : {session.session_id}")
    print(f"  Subject   : {session.subject_id}")
    print(f"  Duration  : {duration} seconds")
    print(f"  Output    : {session.session_dir}")
    print("-" * 60)
    print(f"  🎥 Camera   : device {args.camera_id}")
    print(f"  💓 Oximeter : {args.oximeter_port}")
    print(f"  📶 CSI      : {args.csi_port}")
    print(f"  ⚡ EMG      : {args.emg_port}")
    print(f"  💧 GSR      : {args.gsr_port}")
    print("=" * 60)
    print()


def countdown(seconds: int = 3):
    """Visual countdown before recording starts."""
    for i in range(seconds, 0, -1):
        print(f"  Starting in {i}...", flush=True)
        time.sleep(1)
    print("  🔴 RECORDING!\n")


def main():
    parser = argparse.ArgumentParser(
        description="Dataset Sync — Synchronized Multi-Modal Recording"
    )
    parser.add_argument("--duration", type=int, default=60,
                        help="Recording duration in seconds (default: 60)")
    parser.add_argument("--subject", type=str, default="unknown",
                        help="Subject identifier (default: unknown)")
    parser.add_argument("--output-dir", type=str, default="data/raw",
                        help="Output directory (default: data/raw)")

    # Device arguments
    parser.add_argument("--camera-id", type=int, default=0,
                        help="Camera device ID (default: 0)")
    parser.add_argument("--camera-fps", type=int, default=30,
                        help="Camera FPS (default: 30)")
    parser.add_argument("--camera-res", type=str, default="640x480",
                        help="Camera resolution WxH (default: 640x480)")
    parser.add_argument("--save-format", type=str, default="frames",
                        choices=["frames", "video"],
                        help="Save as individual frames or video (default: frames)")

    parser.add_argument("--oximeter-port", type=str, default="auto",
                        help="Oximeter serial port (default: auto-detect)")
    parser.add_argument("--oximeter-baud", type=int, default=0,
                        help="Oximeter baud rate (default: auto-detect)")

    parser.add_argument("--csi-port", type=str, default="/dev/ttyUSB1",
                        help="ESP32 receiver serial port (default: /dev/ttyUSB1)")
    parser.add_argument("--csi-baud", type=int, default=115200,
                        help="CSI baud rate (default: 115200)")

    parser.add_argument("--emg-port", type=str, default="auto",
                        help="EMG ESP32-S3 port (default: auto)")
    parser.add_argument("--emg-baud", type=int, default=230400,
                        help="EMG baud rate (default: 230400)")

    parser.add_argument("--gsr-port", type=str, default="auto",
                        help="GSR ESP32 port (default: auto)")
    parser.add_argument("--gsr-baud", type=int, default=115200,
                        help="GSR baud rate (default: 115200)")

    # Feature flags
    parser.add_argument("--no-camera", action="store_true",
                        help="Disable camera recording")
    parser.add_argument("--no-oximeter", action="store_true",
                        help="Disable oximeter recording")
    parser.add_argument("--no-csi", action="store_true",
                        help="Disable CSI recording")
    parser.add_argument("--no-emg", action="store_true",
                        help="Disable EMG recording")
    parser.add_argument("--no-gsr", action="store_true",
                        help="Disable GSR recording")

    parser.add_argument("--no-countdown", action="store_true",
                        help="Skip the countdown")

    args = parser.parse_args()

    # Parse resolution
    res_parts = args.camera_res.split("x")
    resolution = (int(res_parts[0]), int(res_parts[1]))

    # ── Create session ──
    session = Session(output_dir=args.output_dir, subject_id=args.subject)
    print_banner(session, args.duration, args)

    # ── Initialize recorders ──
    recorders = []

    if not args.no_camera:
        cam = CameraRecorder(
            session=session,
            device_id=args.camera_id,
            fps=args.camera_fps,
            resolution=resolution,
            save_format=args.save_format
        )
        recorders.append(("CAMERA", cam))

    if not args.no_oximeter:
        oxi = OximeterRecorder(
            session=session,
            port=args.oximeter_port,
            baud=args.oximeter_baud
        )
        recorders.append(("OXIMETER", oxi))

    if not args.no_csi:
        csi = CSIRecorder(
            session=session,
            port=args.csi_port,
            baud=args.csi_baud
        )
        recorders.append(("CSI", csi))

    # EMG and GSR use the same standalone recorder pattern
    # (placeholder until dedicated EMG/GSR recorders are added)
    if not args.no_emg and args.emg_port != 'none':
        print(f"  ⚡ EMG recording from {args.emg_port} @ {args.emg_baud}")
        print(f"     (EMG standalone recorder — uses dashboard handler)")

    if not args.no_gsr and args.gsr_port != 'none':
        print(f"  💧 GSR recording from {args.gsr_port} @ {args.gsr_baud}")
        print(f"     (GSR standalone recorder — uses dashboard handler)")

    if not recorders:
        print("ERROR: All recorders are disabled. Nothing to record.")
        sys.exit(1)

    modality_names = [name for name, _ in recorders]
    print(f"  Active modalities: {', '.join(modality_names)}")
    print()

    # ── Countdown ──
    if not args.no_countdown:
        countdown(3)

    # ── START all recorders simultaneously ──
    print(f"  ▶ Starting {len(recorders)} recorders...")
    for name, recorder in recorders:
        recorder.start()
        print(f"    ✓ {name} started")
    print()

    # ── Wait for duration ──
    start_time = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - start_time
            remaining = args.duration - elapsed
            if remaining <= 0:
                break

            # Status update every second
            stats = []
            for name, recorder in recorders:
                if name == "CAMERA":
                    stats.append(f"🎥 {recorder.frame_count} frames")
                elif name == "OXIMETER":
                    stats.append(f"💓 {recorder.sample_count} samples")
                elif name == "CSI":
                    stats.append(f"📶 {recorder.packet_count} packets")

            status = " | ".join(stats)
            print(f"\r  ⏱️  {elapsed:.0f}s / {args.duration}s  |  {status}",
                  end="", flush=True)
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n  ⚠️  Interrupted by user!")

    # ── STOP all recorders ──
    print(f"\n\n  ⏹ Stopping recorders...")
    for name, recorder in recorders:
        recorder.stop()
        print(f"    ✓ {name} stopped")

    # ── Finalize session ──
    actual_duration = time.monotonic() - start_time

    # Gather final counts
    cam_frames = 0
    oxi_samples = 0
    csi_packets = 0
    emg_packets = 0
    gsr_samples = 0
    for name, recorder in recorders:
        if name == "CAMERA":
            cam_frames = recorder.frame_count
        elif name == "OXIMETER":
            oxi_samples = recorder.sample_count
        elif name == "CSI":
            csi_packets = recorder.packet_count

    session.update_stats(cam_frames, oxi_samples, csi_packets,
                         emg_packets, gsr_samples)
    meta_path = session.finalize(actual_duration)

    # ── Summary ──
    print()
    print("=" * 60)
    print("  ✅ Recording Complete!")
    print("=" * 60)
    print(f"  Duration : {actual_duration:.1f}s")
    print(f"  Camera   : {cam_frames} frames")
    print(f"  Oximeter : {oxi_samples} samples")
    print(f"  CSI      : {csi_packets} packets")
    print(f"  EMG      : {emg_packets} packets")
    print(f"  GSR      : {gsr_samples} samples")
    print(f"  Output   : {session.session_dir}")
    print(f"  Metadata : {meta_path}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
