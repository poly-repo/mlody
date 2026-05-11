#!/usr/bin/env bash
set -euo pipefail
ROOT="$0.runfiles/_main/mlody/stage"
cd "$ROOT"
exec python3 -m http.server "${PORT:-8000}"
