#!/usr/bin/env bash
# One-command local dev launcher against the REAL Azure OpenAI deployment
# (gpt-5-mini) instead of the local Ollama fallback -- needed to exercise
# V2's tool-calling agent at all, since Ollama doesn't support it (SPEC-M16).
#
# Requires `az login` to already be active with an identity that has the
# "Cognitive Services OpenAI User" role on the armie-m3-openai resource --
# DefaultAzureCredential falls through to your az-cli login locally, no API
# key needed. Starts the API (uvicorn) and the web app (vite) together,
# stops both on Ctrl+C. Dev tooling only -- does not touch app code.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "warning: .venv not found -- run:" >&2
  echo "  python3.12 -m venv .venv && source .venv/bin/activate && pip install -e 'apps/api[dev]'" >&2
fi

if ! az account show >/dev/null 2>&1; then
  echo "error: not logged in to Azure CLI -- run 'az login' first (DefaultAzureCredential needs it locally)." >&2
  exit 1
fi

export LLM_PROVIDER=azure
export AZURE_OPENAI_ENDPOINT=https://armie-m3-openai.openai.azure.com/
export AZURE_OPENAI_API_VERSION=2024-10-21
export AZURE_OPENAI_TEXT_DEPLOYMENT=gpt-5-mini
export AZURE_OPENAI_VISION_DEPLOYMENT=gpt-5-mini

trap 'echo; echo "Stopping..."; kill 0' EXIT INT TERM

PYTHONPATH=apps/api python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 &
( cd apps/web && npm run dev ) &

echo "API:   http://127.0.0.1:8000  (real Azure gpt-5-mini -- expect real model latency)"
echo "Web:   see Vite output above (usually http://127.0.0.1:5173)"
echo "Note:  local backend has no ADLS configured, so it only knows project \"demo\" --"
echo "       apps/web/src/main.tsx's projectId default must be \"demo\" for local testing"
echo "       (it's \"duplex\" in the committed/production version; don't commit \"demo\")."
echo "Press Ctrl+C to stop both."
wait
