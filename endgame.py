from __future__ import annotations
import ctypes
import ctypes.wintypes as W
import json
import pathlib
import re
import sys
import time
from datetime import datetime

import autonomous_self_correct
import log as logger
from log import log
from prompt_sections import (
    load_prompt, call_llm, assemble_prompt,
    section_goal, section_screen, section_actor_history, section_verified_state,
    section_available_windows, section_progress, section_next_step,
    ACTOR_HISTORY_PATH, INTERACTION_LOG_PATH, VERIFIED_STATE_PATH,
)
from collector import pipeline as collect_pipeline
from render import pipeline as render_pipeline

BASE_PATH = pathlib.Path(__file__).parent
LESSONS_PATH = BASE_PATH / "lessons.txt"
LESSONS_DIR = BASE_PATH / "lessons"

# ── Timing constants (seconds) ──
DELAY_STARTUP = 5                # Time for user to switch to target app before agent starts
DELAY_FOCUS = 0.3                # Wait after SetForegroundWindow for OS to process focus switch
DELAY_CURSOR_SETTLE = 0.05       # Wait after SetCursorPos for position to register
DELAY_MOUSE_HOLD = 0.05          # Hold duration between mouse down/up for reliable click
DELAY_DOUBLECLICK_GAP = 0.05     # Gap between first and second click (OS double-click threshold)
DELAY_KEY_INTER = 0.03           # Between each key down/up event for modifier ordering
DELAY_CHAR_SEND = 0.03           # Between each character in SendInput for apps to process
DELAY_TYPE_FOCUS = 0.3           # After clicking text field, wait for cursor/focus to appear
DELAY_BETWEEN_ACTIONS = 0.5      # Between actions in a chain, let UI react
DELAY_BETWEEN_CYCLES = 2.0       # Between cycles, let last action's effect fully render
DELAY_MANUAL_RESTORE = 0.3       # After restoring focus from manual input pause
TREE_WALK_TIMEOUT = 5.0          # Max seconds to walk a window's UI Automation tree
PROBE_STEP_PX = 50               # Pixel grid step for probe scan across focused window

act_user32 = ctypes.WinDLL("user32", use_last_error=True)
act_user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

VK_MAP: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "super": 0x5B, "space": 0x20,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
} | {chr(ord("a") + i): ord("A") + i for i in range(26)} | {chr(ord("0") + i): ord("0") + i for i in range(10)}

EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("_pad", ctypes.c_ubyte * 32)]
    _fields_ = [("type", W.DWORD), ("u", _U)]


def click(hwnd: int, px: int, py: int) -> None:
    act_user32.SetForegroundWindow(hwnd)
    time.sleep(DELAY_FOCUS)
    act_user32.SetCursorPos(px, py)
    time.sleep(DELAY_CURSOR_SETTLE)
    act_user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(DELAY_MOUSE_HOLD)
    act_user32.mouse_event(0x0004, 0, 0, 0, 0)


def double_click(hwnd: int, px: int, py: int) -> None:
    click(hwnd, px, py)
    time.sleep(DELAY_DOUBLECLICK_GAP)
    act_user32.SetCursorPos(px, py)
    act_user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(DELAY_MOUSE_HOLD)
    act_user32.mouse_event(0x0004, 0, 0, 0, 0)


def resolve_vk(name: str) -> int:
    vk = VK_MAP.get(name)
    if vk is not None:
        return vk
    assert len(name) == 1, f"unknown key: {name}"
    scan = act_user32.VkKeyScanW(name)
    assert scan != -1, f"unmappable key: {name}"
    return scan & 0xFF


def press(keys_str: str) -> None:
    parts = [p.strip().lower() for p in keys_str.replace(",", "+").split("+") if p.strip()]
    vk_codes = [resolve_vk(name) for name in parts]
    for vk in vk_codes:
        act_user32.keybd_event(vk, 0, 0x0001 if vk in EXTENDED_VKS else 0, None)
        time.sleep(DELAY_KEY_INTER)
    for vk in reversed(vk_codes):
        act_user32.keybd_event(vk, 0, 0x0002 | (0x0001 if vk in EXTENDED_VKS else 0), None)
        time.sleep(DELAY_KEY_INTER)


def send_text(text: str) -> None:
    for char in text:
        code = ord(char)
        inputs = (INPUT * 2)()
        inputs[0].type = 1
        inputs[0].u.ki.wScan = code
        inputs[0].u.ki.dwFlags = 0x0004
        inputs[1].type = 1
        inputs[1].u.ki.wScan = code
        inputs[1].u.ki.dwFlags = 0x0006
        act_user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        time.sleep(DELAY_CHAR_SEND)


def resolve_selector(selector: str, book: dict[str, dict]) -> tuple[int, int, int, str, str]:
    entry = book.get(selector)
    assert entry, f"id not in book: {selector}"
    return (entry["px"] + entry["pw"] // 2, entry["py"] + entry["ph"] // 2,
            entry["hwnd"], entry["role"], entry["name"])


def validate_action(action_str: str, book: dict[str, dict]) -> str | None:
    verb = action_str.split()[0].rstrip(":;,")
    rest = action_str[len(action_str.split()[0]):].strip()
    valid_ids = set(book.keys())
    match verb:
        case "click" | "double_click":
            node_id = rest.split()[0]
            if not node_id.isdigit():
                return f"{verb} requires a numeric ID, got: {rest}"
            if node_id not in valid_ids:
                return f"ID {node_id} not in screen. Available: {sorted(valid_ids, key=int)}"
        case "type":
            m = re.match(r"(\d+)\s+(.+)", rest)
            if not m:
                return f"type requires 'ID text' format, got: {rest}"
            if m.group(1) not in valid_ids:
                return f"ID {m.group(1)} not in screen. Available: {sorted(valid_ids, key=int)}"
        case "press":
            if not rest:
                return "press requires key name(s)"
        case "done":
            return None
        case _:
            return f"unknown action: {verb}. Valid: click, double_click, type, press, done"
    return None


def phase_act(action_str: str, book: dict[str, dict]) -> tuple[str, dict]:
    verb = action_str.split()[0].rstrip(":;,")
    rest = action_str[len(action_str.split()[0]):].strip()
    match verb:
        case "click" | "double_click":
            node_id = rest.split()[0]
            px, py, hwnd, role, name = resolve_selector(node_id, book)
            (double_click if verb == "double_click" else click)(hwnd, px, py)
            result = f"{verb}ed {node_id} at ({px},{py})"
            interaction = {"action": verb, "hwnd": hwnd, "px": px, "py": py,
                           "role": role, "name": name, "element_id": node_id}
        case "type":
            m = re.match(r"(\d+)\s+(.+)", rest)
            assert m, f"no ID in type action: {rest}"
            node_id, text = m.group(1), m.group(2)
            px, py, hwnd, role, name = resolve_selector(node_id, book)
            click(hwnd, px, py)
            time.sleep(DELAY_TYPE_FOCUS)
            send_text(text)
            result = f"typed '{text}' into {node_id}"
            interaction = {"action": "type", "hwnd": hwnd, "px": px, "py": py,
                           "role": role, "name": name, "element_id": node_id, "typed": text}
        case "press":
            keys = rest.split()[0]
            press(keys)
            result = f"pressed {keys}"
            interaction = {"action": "press", "keys": keys, "hwnd": 0, "px": 0, "py": 0,
                           "role": "", "name": ""}
        case _:
            assert False, f"unknown verb: {verb}"
    return result, interaction


def phase_collect(expand_hwnds: list[int] | None) -> list[str]:
    return collect_pipeline(TREE_WALK_TIMEOUT, PROBE_STEP_PX, False, expand_hwnds)


def phase_render(raw_lines: list[str]) -> tuple[str, dict[str, dict]]:
    context_text, book_entries = render_pipeline(raw_lines)
    return context_text, {e["id"]: e for e in book_entries}


def phase_planner(goal: str, backend: str, raw_lines: list[str]) -> dict:
    user_prompt = assemble_prompt(
        section_goal(goal), section_actor_history(),
        section_verified_state(), section_available_windows(raw_lines),
    )
    parsed = call_llm(load_prompt("planner_system_prompt.txt"), user_prompt, backend, "planner")
    raw_hwnds = parsed.get("expand_hwnds") or parsed.get("windows_to_expand") or []
    return {
        "expand_hwnds": [int(h) for h in raw_hwnds if str(h).isdigit()],
        "done_so_far": parsed.get("done_so_far") or parsed.get("reasoning") or parsed.get("progress") or "",
        "next_step": parsed.get("next_step") or parsed.get("next") or "",
    }


def phase_think(goal: str, planner_output: dict, backend: str, context_text: str) -> dict:
    user_prompt = assemble_prompt(
        section_screen(context_text), section_goal(goal),
        section_progress(planner_output), section_next_step(planner_output),
    )
    parsed = call_llm(load_prompt("actor_system_prompt.txt"), user_prompt, backend, "actor")
    actions = parsed.get("actions") or []
    if not actions:
        single = parsed.get("action") or ""
        actions = [single] if single else []
    return {
        "observe": parsed.get("observe") or parsed.get("observation") or "",
        "reason": parsed.get("reason") or parsed.get("reasoning") or "",
        "actions": actions,
        "expect": parsed.get("expect") or parsed.get("expectation") or parsed.get("expected") or "",
        "extended_observation_needed": parsed.get("extended_observation_needed", False),
        "developer_feedback": parsed.get("developer_feedback") or "",
    }


def save_lesson(goal: str, cycle: int, actor_history: str, verified_state: str) -> None:
    LESSONS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LESSONS_DIR / f"lesson_{timestamp}.json"
    path.write_text(json.dumps({
        "timestamp": timestamp, "goal": goal, "cycle": cycle,
        "actor_history": actor_history, "verified_state": verified_state,
    }, indent=2), encoding="utf-8")


def _read_state() -> tuple[str, str]:
    history = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip() if ACTOR_HISTORY_PATH.exists() else ""
    vs = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip() if VERIFIED_STATE_PATH.exists() else ""
    return history, vs


def _run_self_correct(goal: str, backend: str, apply: bool) -> dict:
    return autonomous_self_correct.run(goal, backend, apply=apply)


def evolve(args: list[str]) -> None:
    backend = args[0] if args else "lmstudio"
    LESSONS_DIR.mkdir(exist_ok=True)
    lesson_files = sorted(LESSONS_DIR.glob("lesson_*.json"))
    if not lesson_files:
        if ACTOR_HISTORY_PATH.exists() and ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip():
            goal = "aggregate"
            if LESSONS_PATH.exists():
                for line in LESSONS_PATH.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        goal = json.loads(line).get("goal", goal)
            _run_self_correct(goal, backend, apply=True)
            return
        return
    all_histories: list[str] = []
    goal = "aggregate"
    for lf in lesson_files:
        data = json.loads(lf.read_text(encoding="utf-8"))
        goal = data.get("goal", goal)
        if data.get("actor_history"):
            all_histories.append(f"--- Run at cycle {data['cycle']} ---\n{data['actor_history']}")
    ACTOR_HISTORY_PATH.write_text("\n".join(all_histories), encoding="utf-8")
    VERIFIED_STATE_PATH.write_text(
        json.loads(lesson_files[-1].read_text(encoding="utf-8")).get("verified_state", ""), encoding="utf-8")
    _run_self_correct(goal, backend, apply=True)
    archive_dir = LESSONS_DIR / "processed"
    archive_dir.mkdir(exist_ok=True)
    for lf in lesson_files:
        lf.rename(archive_dir / lf.name)


def main() -> None:
    time.sleep(DELAY_STARTUP)
    sys.stdout.reconfigure(encoding="utf-8")

    args = sys.argv[1:]
    flags = {"--apply", "--evolve", "--manual"}
    apply_flag = "--apply" in args
    evolve_flag = "--evolve" in args
    manual = "--manual" in args
    autonomous = False
    reflect_every = 2
    if "--autonomous" in args:
        autonomous = True
        idx = args.index("--autonomous")
        if idx + 1 < len(args) and args[idx + 1].isdigit():
            reflect_every = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        else:
            args = args[:idx] + args[idx + 1:]
    args = [a for a in args if a not in flags]

    logger.init()

    if evolve_flag:
        evolve(args)
        logger.close()
        return

    goal = args[0] if len(args) > 0 else "describe what you see"
    max_cycles = int(args[1]) if len(args) > 1 else 10
    backend = args[2] if len(args) > 2 else "lmstudio"

    INTERACTION_LOG_PATH.write_text("", encoding="utf-8")
    ACTOR_HISTORY_PATH.write_text("", encoding="utf-8")

    expand_hwnds: list[int] | None = None
    last_cycle = 0
    try:
      for cycle in range(1, max_cycles + 1):
        last_cycle = cycle
        if manual:
            saved_hwnd = act_user32.GetForegroundWindow()
            saved_pos = W.POINT()
            act_user32.GetCursorPos(ctypes.byref(saved_pos))
            choice = input(f"\n[MANUAL] Cycle {cycle} — Enter=continue, r=reflect: ")
            if choice.strip().lower() == "r":
                _run_self_correct(goal, backend, apply=apply_flag)
                choice = input("[MANUAL] Enter=continue, e=evolve: ")
                if choice.strip().lower() == "e":
                    _run_self_correct(goal, backend, apply=True)
            act_user32.SetForegroundWindow(saved_hwnd)
            act_user32.SetCursorPos(saved_pos.x, saved_pos.y)
            time.sleep(DELAY_MANUAL_RESTORE)
        log(f"\n{'='*60}\nCYCLE {cycle}\n{'='*60}")
        raw_lines = phase_collect(expand_hwnds)
        planner_output = phase_planner(goal, backend, raw_lines)
        planner_expand = planner_output.get("expand_hwnds", [])
        if planner_expand:
            raw_lines = phase_collect(planner_expand)
        context_text, book = phase_render(raw_lines)
        if autonomous and cycle > 1 and cycle % reflect_every == 0:
            if apply_flag:
                _run_self_correct(goal, backend, apply=True)
            else:
                save_lesson(goal, cycle, *_read_state())
        response = phase_think(goal, planner_output, backend, context_text)
        if "done" in response["actions"]:
            response["actions"] = response["actions"][:response["actions"].index("done") + 1]
        chain_ok = True
        for i, action_str in enumerate(response["actions"]):
            if action_str == "done":
                with open(ACTOR_HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(response) + "\n")
                log("\n*** DONE ***")
                chain_ok = False
                break
            error = validate_action(action_str, book)
            if error:
                response["validation_error"] = error
                with open(ACTOR_HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(response) + "\n")
                if autonomous:
                    _run_self_correct(goal, backend, apply=apply_flag)
                chain_ok = False
                break
            _, interaction = phase_act(action_str, book)
            interaction["cycle"] = cycle
            with open(INTERACTION_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(interaction) + "\n")
            time.sleep(DELAY_BETWEEN_ACTIONS)
        if not chain_ok and "done" in response.get("actions", []):
            break
        if not chain_ok:
            expand_hwnds = planner_expand
            time.sleep(DELAY_BETWEEN_CYCLES)
            continue
        with open(ACTOR_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(response) + "\n")
        expand_hwnds = planner_expand if response.get("extended_observation_needed") else None
        time.sleep(DELAY_BETWEEN_CYCLES)
    except Exception as exc:
        log(f"\n[CRASH] Cycle {last_cycle}: {type(exc).__name__}: {exc}")
        history, vs = _read_state()
        if history:
            save_lesson(goal, last_cycle, history, vs)
        raise

    if autonomous:
        if apply_flag:
            _run_self_correct(goal, backend, apply=True)
        else:
            save_lesson(goal, max_cycles, *_read_state())

    logger.close()


if __name__ == "__main__":
    main()
