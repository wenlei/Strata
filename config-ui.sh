#!/usr/bin/env sh
# Strata's settings page: install and switch models, network access, start and stop.
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
  exec .venv/bin/python tools/strata_config_ui.py "$@"
fi
exec python3 tools/strata_config_ui.py "$@"
