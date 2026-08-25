#!/usr/bin/env bash
# One-command local dev launcher: starts the API (uvicorn) and the web app (vite)
# together, stops both on Ctrl+C. Dev tooling only -- does not touch app code.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "warning: .venv not found -- run:" >&2
  echo "  python3.12 -m venv .venv && source .venv/bin/activate && pip install -e 'apps/api[dev]'" >&2
fi

trap 'echo; echo "Stopping..."; kill 0' EXIT INT TERM

PYTHONPATH=apps/api python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
( cd apps/web && npm run dev ) &

echo "API: http://127.0.0.1:8000"
echo "Web: see Vite output above (usually http://127.0.0.1:5173)"
echo "Press Ctrl+C to stop both."
wait
