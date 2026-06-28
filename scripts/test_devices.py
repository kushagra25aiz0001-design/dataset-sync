#!/usr/bin/env python3
"""
Test script for individual device verification.

Supports all 5 sensor modalities with protocol-specific probing:
  - Camera: OpenCV capture test
  - Oximeter: CP210x VID/PID + passive data detection
  - CSI: CSV data line detection
  - EMG: Binary sync pattern detection (0xC7 0x7C)
  - GSR: JSON line detection ({"uS":..., "raw":...})

Usage:
    python scripts/test_devices.py --all
    python scripts/test_devices.py --camera --emg
    python scripts/test_devices.py --emg --emg-port /dev/ttyACM0
"""

import sys
import os
import time
import argparse

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_camera(device_id: int = 0, duration: int = 5):
    """Quick camera test — capture a few frames."""
    import cv2

    print("\n" + "=" * 50)
    print("  🎥 Camera Test")
    print("=" * 50)

    cap = cv2.VideoCapture(device_id)
    if not cap.isOpened():
        print(f"  ❌ Cannot open camera device {device_id}")
        print(f"  Check: ls /dev/video*")
        return False

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    print(f"  Resolution: {w}x{h}")
    print(f"  FPS: {fps}")

    frame_count = 0
    start = time.time()
    while time.time() - start < duration:
        ret, frame = cap.read()
        if ret:
            frame_count += 1

    cap.release()
    actual_fps = frame_count / duration
    print(f"  Captured: {frame_count} frames in {duration}s "
          f"({actual_fps:.1f} FPS)")
    print(f"  ✅ Camera working!")
    return True


def test_oximeter(port: str):
    """Test oximeter — check for CP210x and high-bit data."""
    import serial
    import serial.tools.list_ports

    print("\n" + "=" * 50)
    print("  💓 Oximeter Test")
    print("=" * 50)

    # Check VID/PID
    for p in serial.tools.list_ports.comports():
        if p.device == port:
            vid = f"VID={p.vid:04X}" if p.vid else "VID=?"
            pid = f"PID={p.pid:04X}" if p.pid else "PID=?"
            print(f"  Port: {port} ({p.description}) [{vid} {pid}]")
            if p.vid == 0x10C4 and p.pid == 0xEA60:
                print(f"  ✅ Silicon Labs CP210x detected (Contec oximeter)")
            break

    try:
        ser = serial.Serial(port, 9600, timeout=3)
        ser.reset_input_buffer()
        data = ser.read(30)
        ser.close()
        if data and any(b >= 128 for b in data):
            print(f"  ✅ Oximeter responding ({len(data)} bytes, "
                  f"high-bit data detected)")
            return True
        elif data:
            print(f"  ⚠️  Got {len(data)} bytes but no high-bit data — "
                  f"device may need setup")
            return True
        else:
            print(f"  ⚠️  No data — turn ON device and insert finger")
            return False
    except serial.SerialException as e:
        print(f"  ❌ {e}")
        return False


def test_csi(port: str, baud: int = 115200):
    """Test CSI receiver — look for CSV data lines."""
    import serial

    print("\n" + "=" * 50)
    print("  📶 CSI Receiver Test")
    print("=" * 50)

    try:
        ser = serial.Serial(port, baud, timeout=3)
        ser.reset_input_buffer()
        time.sleep(0.5)

        lines_read = 0
        data_lines = 0
        for _ in range(20):
            line = ser.readline()
            if not line:
                continue
            lines_read += 1
            decoded = line.decode('utf-8', errors='ignore').strip()
            if decoded and decoded[0].isdigit() and decoded.count(',') > 10:
                data_lines += 1
                if data_lines == 1:
                    fields = len(decoded.split(','))
                    print(f"  Sample line: {decoded[:60]}...")
                    print(f"  Fields per line: {fields}")

        ser.close()
        if data_lines > 0:
            print(f"  ✅ CSI receiver streaming ({data_lines} data lines)")
            return True
        elif lines_read > 0:
            print(f"  ⚠️  Got {lines_read} lines but no CSI data — "
                  f"check ESP32 TX/RX pair")
            return False
        else:
            print(f"  ❌ No data from CSI receiver")
            return False
    except serial.SerialException as e:
        print(f"  ❌ CSI: {e}")
        return False


def test_emg(port: str, baud: int = 230400):
    """Test EMG sensor — look for binary sync pattern 0xC7 0x7C."""
    import serial

    print("\n" + "=" * 50)
    print("  ⚡ EMG Sensor Test")
    print("=" * 50)

    # Try WHORU handshake first
    try:
        ser = serial.Serial(port, baud, timeout=2)
        ser.reset_input_buffer()
        ser.write(b"WHORU\n")
        time.sleep(0.3)
        response = ser.read(ser.in_waiting or 50)
        decoded = response.decode('utf-8', errors='ignore')
        if "EMG_S3" in decoded:
            print(f"  ✅ WHORU handshake: identified as EMG_S3")

        # Look for binary sync pattern
        ser.reset_input_buffer()
        time.sleep(0.5)
        data = ser.read(min(ser.in_waiting, 256) or 72)
        ser.close()

        sync_count = 0
        for i in range(len(data) - 1):
            if data[i] == 0xC7 and data[i + 1] == 0x7C:
                sync_count += 1

        if sync_count > 0:
            print(f"  ✅ EMG streaming ({sync_count} sync patterns in "
                  f"{len(data)} bytes)")
            return True
        elif data:
            print(f"  ⚠️  Got {len(data)} bytes but no EMG sync pattern")
            return False
        else:
            print(f"  ❌ No data from EMG sensor")
            return False
    except serial.SerialException as e:
        print(f"  ❌ EMG: {e}")
        return False


def test_gsr(port: str, baud: int = 115200):
    """Test GSR sensor — look for JSON lines with uS/raw keys."""
    import serial

    print("\n" + "=" * 50)
    print("  💧 GSR Sensor Test")
    print("=" * 50)

    # Try WHORU handshake first
    try:
        ser = serial.Serial(port, baud, timeout=2)
        ser.reset_input_buffer()
        ser.write(b"WHORU\n")
        time.sleep(0.3)
        response = ser.read(ser.in_waiting or 50)
        decoded = response.decode('utf-8', errors='ignore')
        if "GSR_ESP" in decoded:
            print(f"  ✅ WHORU handshake: identified as GSR_ESP")

        # Look for JSON data
        ser.reset_input_buffer()
        time.sleep(0.5)
        json_found = False
        for _ in range(10):
            line = ser.readline()
            if not line:
                continue
            text = line.decode('utf-8', errors='ignore').strip()
            if '"uS"' in text and '"raw"' in text:
                print(f"  Sample: {text[:60]}")
                json_found = True
                break

        ser.close()
        if json_found:
            print(f"  ✅ GSR sensor streaming valid JSON data")
            return True
        else:
            print(f"  ⚠️  No JSON GSR data detected")
            return False
    except serial.SerialException as e:
        print(f"  ❌ GSR: {e}")
        return False


def list_serial_ports():
    """List available serial ports with details."""
    import serial.tools.list_ports

    ports = list(serial.tools.list_ports.comports())
    usb_ports = [p for p in ports
                 if 'ttyUSB' in p.device or 'ttyACM' in p.device]

    if usb_ports:
        print(f"\n  Available serial ports:")
        for p in sorted(usb_ports, key=lambda x: x.device):
            vid = f"VID={p.vid:04X}" if p.vid else ""
            pid = f"PID={p.pid:04X}" if p.pid else ""
            ids = f" [{vid} {pid}]" if vid else ""
            print(f"    {p.device}: {p.description}{ids}")
    else:
        print("\n  ⚠️  No USB serial ports found! Check connections.")
    return [p.device for p in usb_ports]


def main():
    parser = argparse.ArgumentParser(description="Test recording devices")
    parser.add_argument("--camera", action="store_true", help="Test camera")
    parser.add_argument("--oximeter", action="store_true", help="Test oximeter")
    parser.add_argument("--csi", action="store_true", help="Test CSI")
    parser.add_argument("--emg", action="store_true", help="Test EMG")
    parser.add_argument("--gsr", action="store_true", help="Test GSR")
    parser.add_argument("--all", action="store_true", help="Test all devices")
    parser.add_argument("--camera-id", type=int, default=0)
    parser.add_argument("--oximeter-port", default="/dev/ttyUSB0")
    parser.add_argument("--csi-port", default="/dev/ttyUSB1")
    parser.add_argument("--emg-port", default="/dev/ttyACM0")
    parser.add_argument("--emg-baud", type=int, default=230400)
    parser.add_argument("--gsr-port", default="/dev/ttyUSB2")
    parser.add_argument("--gsr-baud", type=int, default=115200)
    args = parser.parse_args()

    if not any([args.camera, args.oximeter, args.csi,
                args.emg, args.gsr, args.all]):
        args.all = True

    print("\n" + "=" * 50)
    print("  📡 Dataset Sync — Device Test (5 Modalities)")
    print("=" * 50)

    list_serial_ports()

    results = {}

    if args.camera or args.all:
        results["🎥 Camera"] = test_camera(args.camera_id)

    if args.oximeter or args.all:
        results["💓 Oximeter"] = test_oximeter(args.oximeter_port)

    if args.csi or args.all:
        results["📶 CSI"] = test_csi(args.csi_port)

    if args.emg or args.all:
        results["⚡ EMG"] = test_emg(args.emg_port, args.emg_baud)

    if args.gsr or args.all:
        results["💧 GSR"] = test_gsr(args.gsr_port, args.gsr_baud)

    # Summary
    print("\n" + "=" * 50)
    print("  📋 Summary")
    print("=" * 50)
    for device, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon} {device}")
    print()

    # Overall
    total = len(results)
    passed = sum(1 for ok in results.values() if ok)
    print(f"  Result: {passed}/{total} devices passed")
    print()


if __name__ == "__main__":
    main()
