#!/usr/bin/env bash
# Headless test run (macOS / Linux). Usage: tests/run_tests.sh [/path/to/blender]
set -euo pipefail
BLENDER="${1:-${BLENDER:-blender}}"
cd "$(dirname "$0")/.."
"$BLENDER" -b --factory-startup --python-exit-code 1 --python tests/run_tests.py
