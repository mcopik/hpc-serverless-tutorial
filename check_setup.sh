#!/usr/bin/env bash
# Check your setup for one day of the tutorial. Send the last line to the instructor:
# ALL OK, or FAILED at step N: <reason>.
#
#   ./check_setup.sh            # Thursday
#   ./check_setup.sh friday     # Friday
set -uo pipefail
cd "$(dirname "$0")"
source openwhisk/env.sh

if ! python -c "import sebs" >/dev/null 2>&1; then
  echo "FAILED at step 1: SeBS is not available; in the tutorial directory, run: uv run --project serverless-benchmarks ./check_setup.sh $*"
  exit 1
fi
python tools/check_setup.py "$@" 2>&1 | grep --line-buffered -v -E '^\S*\[[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]+\]'
exit "${PIPESTATUS[0]}"
