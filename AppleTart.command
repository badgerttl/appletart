#!/bin/zsh
cd -- "$(dirname -- "$0")" || exit 1
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m appletart
fi
exec python3 -m appletart
