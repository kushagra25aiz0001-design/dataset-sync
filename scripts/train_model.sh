#!/bin/bash
# ============================================
# Train Model
# ============================================
# Usage: ./scripts/train_model.sh [model_type]
#
# model_type: camera_rppg | csi_rppg | fusion
#
# Example:
#   ./scripts/train_model.sh fusion

MODEL=${1:-"fusion"}

echo "================================================"
echo "  Dataset Sync — Training Model: $MODEL"
echo "================================================"

python -m src.models.train \
    --config config/model_config.yaml \
    --model "$MODEL"
