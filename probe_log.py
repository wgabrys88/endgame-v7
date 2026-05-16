"""Non-invasive probe diagnostic logger. Writes every probe hit and tree node to a separate timestamped file."""
from __future__ import annotations
import pathlib
from datetime import datetime

BASE_PATH = pathlib.Path(__file__).parent
_file = None


def init() -> None:
    global _file
    log_dir = BASE_PATH / "probe-logs"
    log_dir.mkdir(exist_ok=True)
    _file = (log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log").open("a", encoding="utf-8")


def log_probe(px: int, py: int, role: str, name: str, enabled: bool, offscreen: bool) -> None:
    if _file:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _file.write(f"[{ts}] PROBE px={px} py={py} role={role} name={name!r} enabled={enabled} offscreen={offscreen}\n")


def log_tree(wnd: str, depth: int, role: str, name: str, enabled: bool, x: int, y: int, w: int, h: int) -> None:
    if _file:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _file.write(f"[{ts}] TREE wnd={wnd!r} d={depth} role={role} name={name!r} enabled={enabled} x={x} y={y} w={w} h={h}\n")


def log_event(msg: str) -> None:
    if _file:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _file.write(f"[{ts}] EVENT {msg}\n")
        _file.flush()


def close() -> None:
    global _file
    if _file:
        _file.close()
        _file = None
