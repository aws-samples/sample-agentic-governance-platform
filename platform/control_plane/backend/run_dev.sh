#!/bin/bash
# Development startup script for Control Plane backend

# Change to the directory where this script is located
cd "$(dirname "$0")"

echo "Starting Control Plane Backend (Development Mode)..."
echo "Working directory: $(pwd)"
echo ""

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -q -r requirements-dev.txt   # includes requirements.txt via -r

ENV_FILE="${ENV_FILE-.env}"
if [ -n "$ENV_FILE" ]; then
    [ -f "$ENV_FILE" ] || { echo "Error: $ENV_FILE not found (set ENV_FILE to point at one, or empty to run without one)"; exit 1; }
    ENV_ARGS=(--env-file "$ENV_FILE")
else
    ENV_ARGS=()
fi

UVICORN_RELOAD="${UVICORN_RELOAD-1}"
if [ -n "$UVICORN_RELOAD" ]; then
    RELOAD_ARGS=(--reload)
else
    RELOAD_ARGS=()
fi

# Set PYTHONPATH for imports to work
export PYTHONPATH=src

# Start the server
echo ""
echo "Starting FastAPI server..."
echo "API will be available at: http://${AGP_BIND_HOST:-127.0.0.1}:${PORT:-8000}"
echo "API docs: http://${AGP_BIND_HOST:-127.0.0.1}:${PORT:-8000}/docs"
echo ""
exec python -m uvicorn src.main:app "${RELOAD_ARGS[@]}" "${ENV_ARGS[@]}" --host "${AGP_BIND_HOST:-127.0.0.1}" --port "${PORT:-8000}"
