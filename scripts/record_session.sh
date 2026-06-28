#!/bin/bash
# ============================================
# Record Session — Quick Start Script
# ============================================
# Usage: ./scripts/record_session.sh [duration] [subject_id]
#
# Example:
#   ./scripts/record_session.sh 60 subject_01

DURATION=${1:-60}
SUBJECT=${2:-"unknown"}

echo "================================================"
echo "  Dataset Sync — Recording Session"
echo "================================================"
echo "  Duration:  ${DURATION}s"
echo "  Subject:   ${SUBJECT}"
echo "  Started:   $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================"

python -m src.recorder.sync_manager \
    --config config/recording_config.yaml \
    --duration "$DURATION" \
    --subject "$SUBJECT"
