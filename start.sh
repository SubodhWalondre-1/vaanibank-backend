#!/usr/bin/env bash
# VaaniBank AI — Container Startup Script
# Run database migrations, seed baseline data, and start Uvicorn.
set -e

echo "=== VaaniBank AI Startup ==="

# 1. Run database migrations
echo "► Running database migrations (Alembic)..."
alembic upgrade head

# 2. Run column migration
echo "► Running Gujarati and Malayalam column migration..."
python migrate_add_gu_ml_columns.py

# 3. Seed baseline data (if missing)
echo "► Seeding database with branch, teller, and process parameters..."
python seed_data.py

# 4. Start Uvicorn ASGI server
echo "► Starting FastAPI application (port 7860)..."
exec uvicorn main:app --host 0.0.0.0 --port 7860 --ws-ping-interval 20 --ws-ping-timeout 20
