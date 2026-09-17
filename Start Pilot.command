#!/bin/zsh
set -eu
cd -- "$(dirname -- "$0")"
pilot_python='/Library/Frameworks/Python.framework/Versions/3.14/bin/python3'
if [[ ! -x "$pilot_python" ]]; then
    pilot_python="$(command -v python3)"
fi
exec "$pilot_python" launcher.py
