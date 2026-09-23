#!/usr/bin/env bash
set -euo pipefail
repo_root="$(git rev-parse --show-toplevel)"

# Convert all relative file paths to absolute before changing directories
declare -a args
for arg in "$@"; do
  # Skip flags (starting with --)
  if [[ "$arg" == --* ]]; then
    args+=("$arg")
  # Convert relative paths to absolute
  elif [[ "$arg" != /* ]] && [[ -e "$arg" ]]; then
    args+=("$(cd "$(dirname "$arg")" && pwd -P)/$(basename "$arg")")
  # Pass through arguments that look like absolute paths or don't exist yet
  else
    args+=("$arg")
  fi
done

cd "$repo_root"
exec "$repo_root/.venv/bin/python3" -m ruff check --config pyproject.toml "${args[@]}"
