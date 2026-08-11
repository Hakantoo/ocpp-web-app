#!/usr/bin/env bash
# Starts the CSMS, the simulated charger and the dashboard together.
# Ctrl-C stops all three.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$PWD"
export SIM_TIME_SCALE="${SIM_TIME_SCALE:-60}"
export SIM_SAMPLE_INTERVAL="${SIM_SAMPLE_INTERVAL:-5}"

if [ ! -f data/csms.db ]; then
  echo "No database found; creating one."
  python -m csms.db.seed
fi

if [ ! -d frontend/node_modules ]; then
  echo "Installing dashboard dependencies."
  (cd frontend && npm install --no-audit --no-fund)
fi

pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

python -m csms.app & pids+=($!)
sleep 1
python -m simulator.main & pids+=($!)
(cd frontend && npm run dev) & pids+=($!)

echo
echo "  CSMS       http://localhost:9000/docs"
echo "  Simulator  http://localhost:9100/state"
echo "  Dashboard  http://localhost:5173"
echo
wait
