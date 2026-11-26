#!/bin/sh
# Launch the scanner from a source checkout (Linux/macOS).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] && exec .venv/bin/python -m sdrscanner "$@"
exec python3 -m sdrscanner "$@"
