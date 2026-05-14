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
exec python3 - <<'PY'
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path


class NoCacheHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


root = str(Path.cwd())
port = int(os.environ.get("PORT", "8000"))
server = ThreadingHTTPServer(("0.0.0.0", port), partial(NoCacheHandler, directory=root))
server.serve_forever()
PY
