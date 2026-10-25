"""Prism: GPU-accelerated bulk image and video compressor.

Run with:  python prism.py

The first launch installs any missing Python packages (into a local .venv if
the system Python refuses pip installs). FFmpeg is found or fetched on its own.

This file stays small on purpose: image worker processes re-import it on
start-up, so it must not pull in the GUI. The app lives in prism_ui.py and
the engine in prism_engine.py.
"""
import os
import sys
import multiprocessing

MIN_PYTHON = (3, 9)
HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")

REQUIRED = {
    "PIL": "Pillow>=10",
    "customtkinter": "customtkinter>=5.2",
    "av": "av>=12",
    "numpy": "numpy",
    "watchdog": "watchdog",
}
OPTIONAL = {
    "pillow_jxl": "pillow-jxl-plugin",   # JPEG XL output
    "pillow_heif": "pillow-heif",        # HEIC / HEIF input
}
if sys.platform != "win32":
    OPTIONAL["imageio_ffmpeg"] = "imageio-ffmpeg"   # FFmpeg fallback when none is installed


def _fail(msg: str):
    print(f"\nPrism: {msg}\n", file=sys.stderr)
    if sys.platform == "win32" and not sys.stdin.isatty():
        input("Press Enter to close…")
    sys.exit(1)


def _check_python():
    if sys.version_info < MIN_PYTHON:
        _fail(f"Python {'.'.join(map(str, MIN_PYTHON))} or newer is required "
              f"(this is {sys.version.split()[0]}). Get it from https://www.python.org/downloads/")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        hint = {
            "darwin": "brew install python-tk",
            "linux": "sudo apt install python3-tk   (Fedora: sudo dnf install python3-tkinter, Arch: sudo pacman -S tk)",
        }.get("linux" if sys.platform.startswith("linux") else sys.platform,
              "reinstall Python from python.org and tick 'tcl/tk and IDLE'")
        _fail(f"Tkinter is missing from this Python install. Fix: {hint}")


def _missing(mods: dict) -> list:
    import importlib.util
    return [pkg for mod, pkg in mods.items() if importlib.util.find_spec(mod) is None]


def _pip(python: str, packages: list, user: bool = False) -> bool:
    import subprocess
    cmd = [python, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", *(["--user"] if user else []), *packages]
    return subprocess.call(cmd) == 0


def _relaunch_in_venv():
    """System Python won't take pip installs (PEP 668, no pip, ...): use a private .venv instead."""
    import subprocess
    import venv
    win = sys.platform == "win32"
    py = os.path.join(VENV, "Scripts" if win else "bin", "python.exe" if win else "python")
    if not os.path.exists(py):
        print(f"Prism: creating a private environment in {VENV}…")
        try:
            venv.create(VENV, with_pip=True)
        except Exception as e:
            _fail(f"could not create a virtual environment ({e}). "
                  "On Debian/Ubuntu install it with: sudo apt install python3-venv")
    if not _pip(py, list(REQUIRED.values())):
        _fail("could not install " + ", ".join(REQUIRED.values()) + ". Check your internet connection and try again.")
    sys.exit(subprocess.call([py, os.path.abspath(__file__), *sys.argv[1:]]))


def _install(packages: list, in_venv: bool) -> bool:
    return _pip(sys.executable, packages) or (not in_venv and _pip(sys.executable, packages, user=True))


def _ensure_dependencies():
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    required = _missing(REQUIRED)
    if required:
        print(f"Prism: first run, installing {', '.join(required)}…")
        if not _install(required, in_venv):
            if in_venv:
                _fail("could not install " + ", ".join(required) + ". Run: pip install -r requirements.txt")
            _relaunch_in_venv()

    # Optional extras are best effort and only attempted once per Python version,
    # so a package without a wheel for this platform never slows start-up down.
    optional = _missing(OPTIONAL)
    marker = os.path.join(HERE, ".prism-extras")
    stamp = f"{sys.executable}|{sys.version}"
    try:
        with open(marker, encoding="utf-8") as f:
            tried = f.read() == stamp
    except OSError:
        tried = False
    if optional and not tried:
        print(f"Prism: installing optional extras {', '.join(optional)}…")
        for pkg in optional:
            _install([pkg], in_venv)
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(stamp)
        except OSError:
            pass

    import importlib
    import site
    importlib.invalidate_caches()
    try:
        user_site = site.getusersitepackages()
        if os.path.isdir(user_site) and user_site not in sys.path:
            sys.path.append(user_site)
    except Exception:
        pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if not getattr(sys, "frozen", False):
        _check_python()
        _ensure_dependencies()
    from prism_ui import run
    run()
