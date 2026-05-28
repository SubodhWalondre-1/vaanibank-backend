#!/usr/bin/env bash
# VaaniBank AI — Render Build Script
# Installs system deps, Python deps, runs DB migrations, and seeds data

set -o errexit

# Install ffmpeg (needed to convert browser WebM/Opus audio → WAV for STT)
echo "Installing ffmpeg..."
apt-get update -qq && apt-get install -y -qq ffmpeg 2>/dev/null || echo "apt-get failed (read-only FS), ffmpeg may already be available"

# Download Noto fonts for Hindi/Tamil PDF rendering into project dir
echo "Downloading Indic fonts for PDF..."
mkdir -p ./fonts

# Noto Sans Devanagari (Hindi)
curl -sL "https://github.com/google/fonts/raw/main/ofl/notosansdevanagari/NotoSansDevanagari%5Bwdth%2Cwght%5D.ttf" -o ./fonts/NotoSansDevanagari.ttf 2>/dev/null || echo "Failed to download Devanagari font"
# Noto Sans Tamil
curl -sL "https://github.com/google/fonts/raw/main/ofl/notosanstamil/NotoSansTamil%5Bwdth%2Cwght%5D.ttf" -o ./fonts/NotoSansTamil.ttf 2>/dev/null || echo "Failed to download Tamil font"
# Noto Sans (Latin fallback)
curl -sL "https://github.com/google/fonts/raw/main/ofl/notosans/NotoSans%5Bital%2Cwdth%2Cwght%5D.ttf" -o ./fonts/NotoSans.ttf 2>/dev/null || echo "Failed to download NotoSans font"

ls -la ./fonts/ 2>/dev/null || true
echo "Fonts downloaded."

pip install --upgrade pip
pip install -r requirements.txt

# Pre-download RAG models during build phase to avoid runtime delays
echo "Pre-downloading AI models..."
export HF_HOME=$(pwd)/.cache/huggingface
python download_models.py

# Create storage directories
mkdir -p storage/audio storage/summaries

# Run database migrations (creates all tables)
echo "Running Alembic migrations..."
alembic upgrade head

# Run standalone migration to add Gujarati/Malayalam columns
echo "Running Gujarati and Malayalam column migration..."
python migrate_add_gu_ml_columns.py

# Seed initial data (branches, staff, process steps)
echo "Seeding database..."
python seed_data.py

echo "Build complete — VaaniBank AI backend ready"

