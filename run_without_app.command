#!/bin/bash
# Запуск без сборки (нужен Python 3 с tkinter, лучше с python.org)
cd "$(dirname "$0")" || exit 1
PY=/Library/Frameworks/Python.framework/Versions/Current/bin/python3
[ -x "$PY" ] || PY=python3
"$PY" tabletochnitsa.py &
