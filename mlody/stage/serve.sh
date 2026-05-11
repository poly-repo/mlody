#!/usr/bin/env bash
set -euo pipefail

ROOT="$0.runfiles/_main/mlody/stage"
MANIFEST="${RUNFILES_MANIFEST_FILE:-$0.runfiles_manifest}"

if [[ -f "$MANIFEST" ]]; then
  SERVE_ROOT="$(mktemp -d)"
  trap 'rm -rf "$SERVE_ROOT"' EXIT

  while IFS=' ' read -r logical_path physical_path; do
    case "$logical_path" in
      _main/mlody/stage/*)
        relative_path="${logical_path#_main/mlody/stage/}"
        mkdir -p "$SERVE_ROOT/$(dirname "$relative_path")"
        ln -sf "$physical_path" "$SERVE_ROOT/$relative_path"
        ;;
    esac
  done < "$MANIFEST"

  ROOT="$SERVE_ROOT"
fi

cd "$ROOT"
exec python3 -m http.server "${PORT:-8000}"
