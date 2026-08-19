#!/bin/bash
# ============================================
# Record Session — Quick Start Script
# ============================================
# Records via the authoritative headless daemon (camera, oximeter, WiFi CSI, EMG,
# GSR, + optional audio/thermal/ECG), all on one master clock.
#
# Usage: ./scripts/record_session.sh [duration] [subject_id] [extra daemon flags...]
#
# Examples:
#   ./scripts/record_session.sh 60 subject_01
#   ./scripts/record_session.sh 300 subject_01 --audio --audio-device hdmi \
#       --thermal-source screen:100,50,640,480 --marker-port 8181 --console

DURATION=${1:-60}
SUBJECT=${2:-"unknown"}
shift 2 2>/dev/null || true      # remaining args pass through to the daemon

echo "================================================"
echo "  Dataset Sync — Recording Session"
echo "================================================"
echo "  Duration:  ${DURATION}s"
echo "  Subject:   ${SUBJECT}"
echo "  Started:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================"

python -m src.recorder.headless_daemon \
    --duration "$DURATION" \
    --subject "$SUBJECT" \
    "$@"
