"""Launch the installed runtime scripts without changing their import layout."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    spec = find_spec("sharebus_runtime")
    if spec is None or spec.origin is None:
        raise SystemExit("ShareBus runtime is missing; repair this installation.")
    arguments = list(sys.argv[1:] if argv is None else argv)
    gui = not arguments or arguments[0] == "gui"
    script = Path(spec.origin).with_name("launcher.py" if gui else "sharebus.py")
    if not script.is_file():
        raise SystemExit("ShareBus runtime is incomplete; repair this installation.")
    if arguments[:1] == ["gui"]:
        arguments = arguments[1:]
    return subprocess.run([sys.executable, "-B", str(script), *arguments], check=False).returncode
