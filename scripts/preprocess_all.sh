#!/bin/bash
# ============================================
# Preprocess All Sessions
# ============================================

echo "================================================"
echo "  Dataset Sync — Preprocessing All Sessions"
echo "================================================"

for session_dir in data/raw/session_*/; do
    if [ -d "$session_dir" ]; then
        session_name=$(basename "$session_dir")
        echo "Processing: $session_name"
        python -m src.preprocessing.synchronizer --session "$session_dir"
    fi
done

echo "Done! Processed data saved to data/processed/"
