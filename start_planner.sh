#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/cmayfield/code/games/travellers-rest-planner"
PLANNER_PID=""

start_backend() {
    echo "Starting Travellers Rest Planner backend..."
    cd "$ROOT"

    # Use full python path to avoid venv issues
    PYTHON="/home/cmayfield/code/games/travellers-rest-planner/.venv/bin/python"

    # Start backend in background
    "$PYTHON" -m planner --port 8765 > /tmp/planner.log 2>&1 &
    PLANNER_PID=$!
    echo "Backend PID: $PLANNER_PID"

    # Wait for backend to be ready
    for i in $(seq 1 30); do
        if curl -s http://127.0.0.1:8765/ > /dev/null 2>&1; then
            echo "Backend ready."
            return 0
        fi
        sleep 1
    done

    echo "ERROR: Backend failed to start (check /tmp/planner.log)"
    exit 1
}

cleanup() {
    echo "Cleaning up PID $PLANNER_PID..."
    if [ -n "$PLANNER_PID" ] && [ -d "/proc/$PLANNER_PID" ]; then
        kill "$PLANNER_PID" 2>/dev/null || true
        wait "$PLANNER_PID" 2>/dev/null || true
    fi
    echo "Cleanup complete."
}

trap cleanup EXIT INT TERM

start_backend

echo "Planner running at http://127.0.0.1:8765/"
echo "Access http://127.0.0.1:8765/#top for the UI"
echo "Press Ctrl+C to stop or wait for auto-cleanup on crash."

# Wait for the planner process to exit
while ps -p "$PLANNER_PID" > /dev/null 2>&1; do
    sleep 2
done

echo "Planner process has stopped."
exit 0