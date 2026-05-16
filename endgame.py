from __future__ import annotations
import ctypes
import ctypes.wintypes as W
import json
import pathlib
import re
import sys
import time

import autonomous_self_correct
import log as logger
import probe_log
from log import log, history
from llm import set_max_request_tokens
from prompt_sections import (
    load_prompt, call_llm, assemble_prompt,
    section_goal, section_screen, section_actor_history, section_interaction_log,
    section_verified_state, section_available_windows, section_progress,
    section_next_step, section_expanded_data, resolve_expansion,
    section_developer_feedback, section_budget,
)
from collector import pipeline as collect_pipeline
from render import pipeline as render_pipeline

BASE_PATH = pathlib.Path(__file__).parent
INTERACTION_LOG_PATH = BASE_PATH / "interaction_log.jsonl"

DELAY_STARTUP = 5
DELAY_FOCUS = 0.3
DELAY_CURSOR_SETTLE = 0.05
DELAY_MOUSE_HOLD = 0.05
DELAY_DOUBLECLICK_GAP = 0.05
DELAY_KEY_INTER = 0.03
DELAY_CHAR_SEND = 0.03
DELAY_TYPE_FOCUS = 0.3
DELAY_BETWEEN_ACTIONS = 0.5
DELAY_BETWEEN_CYCLES = 5.0
DELAY_MANUAL_RESTORE = 0.3
TREE_WALK_TIMEOUT = 5.0
PROBE_STEP_PX = 50

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
    "`": 0xC0, "~": 0xC0,
    "-": 0xBD, "_": 0xBD,
    "=": 0xBB, "+": 0xBB,
    "[": 0xDB, "{": 0xDB,
    "]": 0xDD, "}": 0xDD,
    "\\": 0xDC, "|": 0xDC,
    ";": 0xBA, ":": 0xBA,
    "'": 0xDE, '"': 0xDE,
    ",": 0xBC, "<": 0xBC,
    ".": 0xBE, ">": 0xBE,
    "/": 0xBF, "?": 0xBF,
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
    entry = book[selector]
    return (entry["px"] + entry["pw"] // 2, entry["py"] + entry["ph"] // 2,
            entry["hwnd"], entry["role"], entry["name"])


def validate_action(action_str: str, book: dict[str, dict]) -> str | None:
    verb = action_str.split()[0].rstrip(":;,")
    rest = action_str[len(action_str.split()[0]):].strip().strip('"\'')
    valid_ids = set(book.keys())
    match verb:
        case "click" | "double_click":
            node_id = rest.split()[0]
            if not node_id.isdigit():
                return f"{verb} requires a numeric ID, got: {rest}"
            if node_id not in valid_ids:
                return f"ID {node_id} not in screen. Available: {sorted(valid_ids, key=int)}"
        case "type" | "replace":
            m = re.match(r"(\d+)\s+(.+)", rest)
            if not m:
                return f"{verb} requires 'ID text' format, got: {rest}"
            if m.group(1) not in valid_ids:
                return f"ID {m.group(1)} not in screen. Available: {sorted(valid_ids, key=int)}"
        case "press":
            if not rest:
                return "press requires key name(s)"
        case "done":
            return None
        case _:
            return f"unknown action: {verb}. Valid: click, double_click, type, replace, press, done"
    return None


def execute_action(action_str: str, book: dict[str, dict]) -> dict:
    verb = action_str.split()[0].rstrip(":;,")
    rest = action_str[len(action_str.split()[0]):].strip().strip('"\'')
    match verb:
        case "click" | "double_click":
            node_id = rest.split()[0]
            px, py, hwnd, role, name = resolve_selector(node_id, book)
            (double_click if verb == "double_click" else click)(hwnd, px, py)
            return {"action": verb, "element_id": node_id, "px": px, "py": py,
                    "hwnd": hwnd, "role": role, "name": name}
        case "type":
            m = re.match(r"(\d+)\s+(.+)", rest)
            assert m
            node_id, text = m.group(1), m.group(2)
            px, py, hwnd, role, name = resolve_selector(node_id, book)
            click(hwnd, px, py)
            time.sleep(DELAY_TYPE_FOCUS)
            send_text(text)
            return {"action": "type", "element_id": node_id, "typed": text,
                    "px": px, "py": py, "hwnd": hwnd, "role": role, "name": name}
        case "replace":
            m = re.match(r"(\d+)\s+(.+)", rest)
            assert m
            node_id, text = m.group(1), m.group(2)
            px, py, hwnd, role, name = resolve_selector(node_id, book)
            click(hwnd, px, py)
            time.sleep(DELAY_TYPE_FOCUS)
            press("ctrl+a")
            time.sleep(DELAY_KEY_INTER)
            send_text(text)
            return {"action": "replace", "element_id": node_id, "typed": text,
                    "px": px, "py": py, "hwnd": hwnd, "role": role, "name": name}
        case "press":
            keys = rest.split()[0]
            press(keys)
            return {"action": "press", "keys": keys, "hwnd": 0, "px": 0, "py": 0,
                    "role": "", "name": ""}
        case _:
            assert False, f"unknown verb: {verb}"


def phase_collect(expand_hwnds: list[int] | None) -> list[str]:
    return collect_pipeline(TREE_WALK_TIMEOUT, PROBE_STEP_PX, expand_hwnds)


def phase_render(raw_lines: list[str]) -> tuple[str, dict[str, dict]]:
    context_text, book_entries = render_pipeline(raw_lines)
    return context_text, {e["id"]: e for e in book_entries}


def phase_planner(goal: str, backend: str, raw_lines: list[str], expanded: list[str] | None = None,
                  cycle: int = 0, max_cycles: int = 0) -> dict:
    hwnd_map: dict[str, int] = {}
    user_prompt = assemble_prompt(
        section_goal(goal), section_budget(cycle, max_cycles),
        section_actor_history(), section_interaction_log(),
        section_verified_state(), section_developer_feedback(),
        section_available_windows(raw_lines, hwnd_map),
        section_expanded_data(expanded or []),
    )
    parsed = call_llm(load_prompt("planner_system_prompt.txt"), user_prompt, backend, "planner")

    # Translate window labels (W1, W2...) back to HWNDs
    raw_windows = parsed.get("expand_windows") or []
    expand_hwnds: list[int] = []
    for w in raw_windows:
        w_str = str(w).strip()
        if w_str in hwnd_map:
            expand_hwnds.append(hwnd_map[w_str])
        elif w_str.isdigit():
            expand_hwnds.append(int(w_str))

    return {
        "expand_hwnds": expand_hwnds,
        "expand_data": parsed.get("expand_data") or [],
        "done_so_far": parsed.get("done_so_far") or "",
        "next_step": parsed.get("next_step") or "",
    }


def phase_think(goal: str, planner_output: dict, backend: str, context_text: str,
                expanded: list[str] | None = None, book: dict[str, dict] | None = None,
                cycle: int = 0, max_cycles: int = 0) -> dict:
    user_prompt = assemble_prompt(
        section_screen(context_text), section_goal(goal),
        section_budget(cycle, max_cycles),
        section_progress(planner_output), section_next_step(planner_output),
        section_expanded_data(expanded or []),
    )
    parsed = call_llm(load_prompt("actor_system_prompt.txt"), user_prompt, backend, "actor")
    return {
        "observe": parsed.get("observe") or "",
        "reason": parsed.get("reason") or "",
        "actions": parsed.get("actions") or [],
        "expect": parsed.get("expect") or "",
        "extended_observation_needed": parsed.get("extended_observation_needed", False),
        "expand_data": parsed.get("expand_data") or [],
        "developer_feedback": parsed.get("developer_feedback") or "",
    }


def run_reflection(goal: str, backend: str, lessons_depth: int, do_evolve: bool) -> None:
    if not do_evolve:
        return
    analysis = autonomous_self_correct.reflect(goal, backend, lessons_depth)
    autonomous_self_correct.evolve(analysis)


def main() -> None:
    time.sleep(DELAY_STARTUP)
    sys.stdout.reconfigure(encoding="utf-8")

    args = sys.argv[1:]
    manual = "--manual" in args
    do_evolve = "--evolve" in args
    reflect_every = 0
    lessons_depth = 1
    max_tokens = None
    if "--req-tokens-max" in args:
        idx = args.index("--req-tokens-max")
        max_tokens = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    if "--reflect" in args:
        idx = args.index("--reflect")
        reflect_every = int(args[idx + 1])
        lessons_depth = reflect_every
        args = args[:idx] + args[idx + 2:]
    args = [a for a in args if a not in ("--manual", "--evolve")]

    set_max_request_tokens(max_tokens)

    goal = args[0] if len(args) > 0 else "describe what you see"
    max_cycles = int(args[1]) if len(args) > 1 else 10
    backend = args[2] if len(args) > 2 else "lmstudio"

    logger.init()
    probe_log.init()
    history("RUN_START", json.dumps({"goal": goal, "cycles": max_cycles, "backend": backend}, ensure_ascii=False))
    INTERACTION_LOG_PATH.write_text("", encoding="utf-8")

    expand_hwnds: list[int] | None = None
    pending_planner_expansions: list[str] = []
    pending_actor_expansions: list[str] = []

    for cycle in range(1, max_cycles + 1):
        if manual:
            saved_hwnd = act_user32.GetForegroundWindow()
            saved_pos = W.POINT()
            act_user32.GetCursorPos(ctypes.byref(saved_pos))
            input(f"\n[MANUAL] Cycle {cycle} — Enter to continue: ")
            act_user32.SetForegroundWindow(saved_hwnd)
            act_user32.SetCursorPos(saved_pos.x, saved_pos.y)
            time.sleep(DELAY_MANUAL_RESTORE)

        log(f"\n{'='*60}\nCYCLE {cycle}\n{'='*60}")
        probe_log.log_event(f"CYCLE {cycle} START")

        raw_lines = phase_collect(expand_hwnds)
        planner_output = phase_planner(goal, backend, raw_lines, pending_planner_expansions,
                                       cycle, max_cycles)
        pending_planner_expansions = []
        history("PLANNER", json.dumps({"done_so_far": planner_output["done_so_far"], "next_step": planner_output["next_step"]}, ensure_ascii=False))

        planner_expand = planner_output.get("expand_hwnds", [])
        if planner_expand:
            raw_lines = phase_collect(planner_expand)
        expand_hwnds = planner_expand or None

        context_text, book = phase_render(raw_lines)

        # Resolve planner's expand_data requests → inject into actor this cycle
        planner_data_reqs = planner_output.get("expand_data") or []
        actor_injections = list(pending_actor_expansions)
        pending_actor_expansions = []
        for req in planner_data_reqs:
            ref = req.get("ref", "")
            rng = req.get("range", [0, 100])
            resolved = resolve_expansion(ref, rng, book)
            log(f"[EXPAND planner] {ref} [{rng[0]}-{rng[1]}%] → {len(resolved)} chars")
            # Planner's data requests go to planner next cycle (it asked for itself)
            pending_planner_expansions.append(resolved)

        if reflect_every and cycle > 1 and cycle % reflect_every == 0:
            run_reflection(goal, backend, lessons_depth, do_evolve)

        response = phase_think(goal, planner_output, backend, context_text, actor_injections, book,
                               cycle, max_cycles)

        if "done" in response.get("actions", []):
            response["actions"] = response["actions"][:response["actions"].index("done") + 1]

        chain_ok = True
        for action_str in response.get("actions", []):
            if action_str == "done":
                history("ACTOR", json.dumps(response, ensure_ascii=False))
                log("\n*** DONE ***")
                chain_ok = False
                break
            error = validate_action(action_str, book)
            if error:
                response["validation_error"] = error
                history("ACTOR", json.dumps(response, ensure_ascii=False))
                chain_ok = False
                break
            interaction = execute_action(action_str, book)
            interaction["cycle"] = cycle
            probe_log.log_event(f"ACTION {action_str} → px={interaction.get('px',0)} py={interaction.get('py',0)}")
            history("ACTION", json.dumps(interaction, ensure_ascii=False))
            with open(INTERACTION_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(interaction) + "\n")
            time.sleep(DELAY_BETWEEN_ACTIONS)

        # Resolve actor's expand_data requests AFTER actions execute
        # (so clipboard/element reads reflect post-action state)
        actor_data_reqs = response.get("expand_data") or []
        for req in actor_data_reqs:
            ref = req.get("ref", "")
            rng = req.get("range", [0, 100])
            resolved = resolve_expansion(ref, rng, book)
            log(f"[EXPAND actor] {ref} [{rng[0]}-{rng[1]}%] → {len(resolved)} chars")
            pending_actor_expansions.append(resolved)

        if not chain_ok and "done" in response.get("actions", []):
            break
        if not chain_ok:
            time.sleep(DELAY_BETWEEN_CYCLES)
            continue

        history("ACTOR", json.dumps(response, ensure_ascii=False))
        if response.get("extended_observation_needed") and not planner_expand:
            expand_hwnds = None
        time.sleep(DELAY_BETWEEN_CYCLES)

    if reflect_every:
        run_reflection(goal, backend, lessons_depth, do_evolve)

    history("RUN_END", json.dumps({"goal": goal}, ensure_ascii=False))

    # Rotate history after run ends — only if evolution happened
    if do_evolve and reflect_every:
        from autonomous_self_correct import _rotate_history
        _rotate_history()

    logger.close()
    probe_log.close()


if __name__ == "__main__":
    main()
