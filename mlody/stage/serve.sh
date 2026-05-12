#!/usr/bin/env bash
set -euo pipefail

ROOT="$0.runfiles/_main/mlody/stage"
AVATAR_ROOT="$0.runfiles/_main/mlody/assets/images/avatars"
MANIFEST="${RUNFILES_MANIFEST_FILE:-$0.runfiles_manifest}"

SERVE_ROOT="$(mktemp -d)"
trap 'rm -rf "$SERVE_ROOT"' EXIT

if [[ -f "$MANIFEST" ]]; then
  while IFS=' ' read -r logical_path physical_path; do
    case "$logical_path" in
      _main/mlody/stage/*)
        relative_path="${logical_path#_main/mlody/stage/}"
        mkdir -p "$SERVE_ROOT/$(dirname "$relative_path")"
        ln -sf "$physical_path" "$SERVE_ROOT/$relative_path"
        ;;
      _main/mlody/assets/images/avatars/*)
        relative_path="${logical_path#_main/mlody/}"
        mkdir -p "$SERVE_ROOT/$(dirname "$relative_path")"
        ln -sf "$physical_path" "$SERVE_ROOT/$relative_path"
        ;;
    esac
  done < "$MANIFEST"
else
  while IFS= read -r stage_path; do
    relative_path="${stage_path#"$ROOT"/}"
    mkdir -p "$SERVE_ROOT/$(dirname "$relative_path")"
    ln -sf "$stage_path" "$SERVE_ROOT/$relative_path"
  done < <(find "$ROOT" -type f | sort)

  if [[ -d "$AVATAR_ROOT" ]]; then
    while IFS= read -r avatar_path; do
      relative_path="assets/images/avatars/${avatar_path##*/}"
      mkdir -p "$SERVE_ROOT/$(dirname "$relative_path")"
      ln -sf "$avatar_path" "$SERVE_ROOT/$relative_path"
    done < <(find "$AVATAR_ROOT" -maxdepth 1 -type f -name '*.png' | sort)
  fi
fi

ROOT="$SERVE_ROOT"

cd "$ROOT"
exec python3 -m http.server "${PORT:-8000}"
