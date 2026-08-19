"""
Baud-rate detector for the ESP32 WiFi-CSI receiver
==================================================
If the receiver's UART baud doesn't match what the interface opens the port with,
every byte is garbage and no CSI line parses — the sensor looks dead even though
it's transmitting. This tool reads the port at each candidate baud, scores how
much the data looks like real CSI text, and tells you the right one. You then pin
it with ``--csi-baud <N>`` so the recorder never has to guess (it opens at a fixed
baud — there is no auto-scan to slow the other sensors' startup).

A valid ESP32 CSI line is a long comma-separated list of integers:

    timestamp_ms, seq, rssi, n_carriers, amp0, amp1, ... amp[n_carriers-1]
    11548786,337844,-61,128,63,2,38,38,36,...          (~132 fields for HT40)

so we score each baud by how many lines have many comma-separated *numeric*
fields, plus the fraction of printable bytes (garbage from a wrong baud is mostly
non-printable).

    python -m src.recorder.baud_detect --port /dev/ttyUSB1
    python -m src.recorder.baud_detect --port /dev/ttyUSB1 --dwell 2.0

The scoring core (`score_serial_text`) is pure and unit-tested; the serial read
needs the hardware.
"""

import argparse
import time
from typing import List, Optional, Tuple

# ESP32 UART baud rates worth trying, most-likely first (CSI firmware is usually
# 115200 stock or 921600 after the throughput reflash).
CANDIDATE_BAUDS = [115200, 921600, 230400, 460800, 1000000, 1500000,
                   2000000, 74880, 57600, 9600]


def score_serial_text(data: bytes, min_fields: int = 40,
                      min_numeric_frac: float = 0.8) -> dict:
    """Score raw serial bytes for how much they look like ESP32 CSI text output.

    Returns {n_bytes, printable_ratio, n_lines, n_csi_lines, csi_frac, sample}.
    A line counts as CSI-like if it has >= `min_fields` comma-separated fields of
    which >= `min_numeric_frac` parse as numbers. Pure function (unit-tested)."""
    n = len(data)
    if n == 0:
        return {'n_bytes': 0, 'printable_ratio': 0.0, 'n_lines': 0,
                'n_csi_lines': 0, 'csi_frac': 0.0, 'sample': ''}
    printable = sum(1 for b in data
                    if b in (9, 10, 13) or 32 <= b < 127)  # tab/nl/cr + ASCII
    text = data.decode('ascii', errors='replace')
    lines = [ln.strip() for ln in text.replace('\r', '\n').split('\n') if ln.strip()]
    n_csi = 0
    sample = ''
    for ln in lines:
        fields = ln.split(',')
        if len(fields) < min_fields:
            continue
        numeric = 0
        for f in fields:
            try:
                float(f.strip())
                numeric += 1
            except ValueError:
                pass
        if numeric / len(fields) >= min_numeric_frac:
            n_csi += 1
            if not sample:
                sample = ln[:80]
    return {'n_bytes': n, 'printable_ratio': round(printable / n, 3),
            'n_lines': len(lines), 'n_csi_lines': n_csi,
            'csi_frac': round(n_csi / max(1, len(lines)), 3), 'sample': sample}


def rank(results: List[dict]) -> Optional[dict]:
    """Best candidate = most CSI-like lines, tie-broken by printable ratio.
    Returns None if nothing produced a single CSI-like line."""
    usable = [r for r in results if 'error' not in r and r.get('n_csi_lines', 0) > 0]
    if not usable:
        return None
    return sorted(usable, key=lambda r: (r['n_csi_lines'], r['printable_ratio']),
                  reverse=True)[0]


def detect_baud(port: str, candidates: List[int] = None,
                dwell_s: float = 1.5) -> Tuple[List[dict], Optional[dict]]:
    """Read `port` at each candidate baud for `dwell_s` seconds and score it.
    Returns (all_results, best_or_None). Requires pyserial + the device."""
    import serial
    candidates = candidates or CANDIDATE_BAUDS
    results = []
    for baud in candidates:
        try:
            ser = serial.Serial(port, baudrate=baud, timeout=0.1)
        except Exception as e:                       # port busy / bad baud / missing
            results.append({'baud': baud, 'error': str(e)[:70]})
            continue
        try:
            ser.reset_input_buffer()
            buf = bytearray()
            t0 = time.monotonic()
            while time.monotonic() - t0 < dwell_s:
                chunk = ser.read(4096)
                if chunk:
                    buf.extend(chunk)
            score = score_serial_text(bytes(buf))
            score['baud'] = baud
            results.append(score)
        finally:
            ser.close()
    return results, rank(results)


def main():
    p = argparse.ArgumentParser(description='Detect the ESP32 CSI receiver baud rate.')
    p.add_argument('--port', required=True, help='Serial port, e.g. /dev/ttyUSB1')
    p.add_argument('--dwell', type=float, default=1.5,
                   help='Seconds to sample each baud (default 1.5)')
    p.add_argument('--bauds', type=int, nargs='+', default=None,
                   help='Override the candidate baud list')
    args = p.parse_args()

    print(f'\nProbing {args.port} — {args.dwell}s per baud...\n')
    print(f"  {'baud':>8}  {'bytes':>7}  {'print%':>6}  {'CSI lines':>9}  sample")
    print('  ' + '-' * 68)
    results, best = detect_baud(args.port, args.bauds, args.dwell)
    for r in results:
        if 'error' in r:
            print(f"  {r['baud']:>8}  {'—':>7}  {'—':>6}  {'—':>9}  ⚠ {r['error']}")
            continue
        flag = '⭐' if best and r['baud'] == best['baud'] else '  '
        print(f"{flag}{r['baud']:>8}  {r['n_bytes']:>7}  "
              f"{r['printable_ratio']*100:>5.0f}%  {r['n_csi_lines']:>9}  "
              f"{r['sample'][:40]}")
    print('  ' + '-' * 68)
    if best:
        print(f"\n  ✅ Detected baud: {best['baud']}  "
              f"({best['n_csi_lines']} CSI-like lines)\n")
        print(f"  Use it (no auto-detection, won't delay other sensors):")
        print(f"    python -m src.dashboard --csi-baud {best['baud']}")
        print(f"    python -m src.recorder.headless_daemon --csi-baud {best['baud']}\n")
    else:
        print("\n  ❌ No baud produced CSI-like lines. The problem is probably NOT "
              "the baud —\n     check the port (--port), wiring/USB, and that the "
              "receiver firmware is running.\n")


if __name__ == '__main__':
    main()
