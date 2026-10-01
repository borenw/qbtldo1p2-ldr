#!/bin/sh
# Rerun every LDR check on the DUT and rebuild the page.
#   ./run_ldr.sh [ADE XL results .rdb of the load-regulation run]
# Needs spectre + virtuoso on PATH (load the Cadence environment first).
# Work data (Spectre runs) goes to $LDR_WORK, default /home/usr1/$USER/ldr_work.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
RDB=${1:-$HOME/myLib/tb_QbtLdo1p2_LoadReg/adexl/results/data/Interactive.1.rdb}
WORK=${LDR_WORK:-/home/usr1/$USER/ldr_work}
mkdir -p "$WORK"
export CDS_LOG_PATH="$WORK/logs"; mkdir -p "$CDS_LOG_PATH"
python3 "$HERE/ldr_run.py" -o "$WORK/work"
python3 "$HERE/ldr_page.py" "$RDB" --viva -o "$WORK/out" --real "$WORK/work/real.json"
echo "page: $WORK/out"
