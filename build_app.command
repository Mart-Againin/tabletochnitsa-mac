#!/bin/bash
# Сборка Tabletochnitsa.app. Запускать на Mac: двойной щелчок
# или в Терминале:  bash build_app.command
cd "$(dirname "$0")" || exit 1

fail() { echo; echo "СБОРКА НЕ УДАЛАСЬ: $1"; echo; read -n 1 -s -r -p "Нажмите любую клавишу…"; exit 1; }

# Python с python.org предпочтительнее: в нём свежий Tk. Системный
# /usr/bin/python3 от Apple идёт со старым Tk 8.5, окна в нём работают плохо.
PY=""
for p in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$p" >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -z "$PY" ] && fail "не найден Python 3. Установите его с https://www.python.org/downloads/macos/"

echo "Python: $PY ($("$PY" --version 2>&1))"
TK=$("$PY" -c "import tkinter; print(tkinter.Tcl().eval('info patchlevel'))" 2>/dev/null)
[ -z "$TK" ] && fail "в этом Python нет tkinter. Установите Python с python.org (или: brew install python-tk)."
echo "Tk: $TK"
case "$TK" in 8.5*) fail "Tk $TK слишком старый (это Python от Apple). Установите Python с python.org.";; esac

"$PY" -m venv .venv || fail "не удалось создать окружение .venv"
.venv/bin/python -m pip install --upgrade pip pyinstaller certifi || fail "не удалось установить PyInstaller (нужен интернет)"
rm -rf build dist
.venv/bin/python -m PyInstaller --noconfirm --clean Tabletochnitsa.spec || fail "PyInstaller завершился с ошибкой"

rm -rf Tabletochnitsa.app
cp -R dist/Tabletochnitsa.app ./ || fail "не удалось скопировать приложение"
echo
echo "ГОТОВО: Tabletochnitsa.app лежит в этой папке."
read -n 1 -s -r -p "Нажмите любую клавишу…"
