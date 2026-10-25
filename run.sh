#!/usr/bin/env sh
# Launch Prism on macOS or Linux. Missing Python packages are installed on first run.
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3.9+ is required."
  case "$(uname -s)" in
    Darwin) echo "Install it with:  brew install python python-tk" ;;
    *)      echo "Install it with:  sudo apt install python3 python3-tk python3-venv   (or your distro's equivalent)" ;;
  esac
  exit 1
fi

exec "$PY" prism.py "$@"
