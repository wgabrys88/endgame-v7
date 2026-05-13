from __future__ import annotations
import pathlib
from datetime import datetime

BASE_PATH = pathlib.Path(__file__).parent
_file = None


def init() -> None:
    global _file
    run_dir = BASE_PATH / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    _file = (run_dir / "log.txt").open("a", encoding="utf-8")


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if _file:
        _file.write(line + "\n")
        _file.flush()


def close() -> None:
    global _file
    if _file:
        _file.close()
        _file = None
