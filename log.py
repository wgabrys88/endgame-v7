from __future__ import annotations
import pathlib
from datetime import datetime

BASE_PATH = pathlib.Path(__file__).parent
HISTORY_PATH = BASE_PATH / "execution_history.txt"
_file = None


def init() -> None:
    global _file
    run_dir = BASE_PATH / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    _file = (run_dir / "log.txt").open("a", encoding="utf-8")


def pretty(s: str) -> str:
    s = s.replace('\\\\', '\x00')
    s = s.replace('\\n', '\n')
    s = s.replace('\\t', '\t')
    s = s.replace('\\"', '"')
    s = s.replace('\x00', '\\')
    return s


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    if _file:
        _file.write(line + "\n")
        _file.flush()


def history(tag: str, data: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{tag}] {data}\n"
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(line)


def read_last_n(tag: str, n: int) -> list[str]:
    if not HISTORY_PATH.exists():
        return []
    lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    matches = [l.split(f"[{tag}] ", 1)[1] for l in lines if f"[{tag}]" in l]
    if n < 0:
        return matches
    return matches[-n:] if n > 0 else []


def read_current_run(tag: str) -> list[str]:
    if not HISTORY_PATH.exists():
        return []
    lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    start_idx = 0
    for i in range(len(lines) - 1, -1, -1):
        if "[RUN_START]" in lines[i]:
            start_idx = i
            break
    return [l.split(f"[{tag}] ", 1)[1] for l in lines[start_idx:] if f"[{tag}]" in l]


def close() -> None:
    global _file
    if _file:
        _file.close()
        _file = None
