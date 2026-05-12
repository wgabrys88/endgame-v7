from __future__ import annotations
import ctypes
import ctypes.wintypes as W
import json
import pathlib
import re
import subprocess
import sys
import time

BASE_PATH = pathlib.Path(__file__).parent
CONTEXT_PATH = BASE_PATH / "context.txt"
BOOK_PATH = BASE_PATH / "book.txt"
REQUEST_PATH = BASE_PATH / "request.txt"
RESPONSE_PATH = BASE_PATH / "response.txt"
INTERACTION_LOG_PATH = BASE_PATH / "interaction_log.jsonl"
ACTOR_HISTORY_PATH = BASE_PATH / "actor_history.jsonl"
VERIFIED_STATE_PATH = BASE_PATH / "verified_state.txt"
RUN_LOG_PATH = BASE_PATH / "run_log.txt"
LESSONS_PATH = BASE_PATH / "lessons.txt"
LESSONS_DIR = BASE_PATH / "lessons"

_run_log_file = None


def log(msg: str) -> None:
    """Print to console AND append to run_log.txt."""
    global _run_log_file
    print(msg)
    if _run_log_file:
        _run_log_file.write(msg + "\n")
        _run_log_file.flush()

# ─── PROMPTS ──────────────────────────────────────────────────────────────────

PROMPTS_DIR = BASE_PATH / "prompts"


def load_prompt(name: str) -> str:
    """Load a prompt from prompts/ directory. Falls back to original/ if missing."""
    path = PROMPTS_DIR / name
    if not path.exists():
        path = PROMPTS_DIR / "original" / name
    return path.read_text(encoding="utf-8").strip()


# ─── INPUT INJECTION ──────────────────────────────────────────────────────────

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
}
for _i in range(26):
    VK_MAP[chr(ord("a") + _i)] = ord("A") + _i
for _i in range(10):
    VK_MAP[chr(ord("0") + _i)] = ord("0") + _i

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
    time.sleep(0.15)
    act_user32.SetCursorPos(px, py)
    time.sleep(0.05)
    act_user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.02)
    act_user32.mouse_event(0x0004, 0, 0, 0, 0)


def double_click(hwnd: int, px: int, py: int) -> None:
    click(hwnd, px, py)
    time.sleep(0.05)
    act_user32.SetCursorPos(px, py)
    act_user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.02)
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
    vk_codes: list[int] = [resolve_vk(name) for name in parts]
    for vk in vk_codes:
        flags = 0x0001 if vk in EXTENDED_VKS else 0
        act_user32.keybd_event(vk, 0, flags, None)
        time.sleep(0.02)
    for vk in reversed(vk_codes):
        flags = 0x0002 | (0x0001 if vk in EXTENDED_VKS else 0)
        act_user32.keybd_event(vk, 0, flags, None)
        time.sleep(0.02)


def send_text(text: str) -> None:
    for char in text:
        code = ord(char)
        inputs = (INPUT * 2)()
        inputs[0].type = 1
        inputs[0].u.ki.wScan = code
        inputs[0].u.ki.dwFlags = 0x0004
        inputs[1].type = 1
        inputs[1].u.ki.wScan = code
        inputs[1].u.ki.dwFlags = 0x0004 | 0x0002
        act_user32.SendInput(2, ctypes.byref(inputs), ctypes.sizeof(INPUT))
        time.sleep(0.02)


# ─── SELECTOR RESOLUTION ─────────────────────────────────────────────────────


def resolve_selector(selector: str) -> tuple[int, int, int, str, str]:
    """Returns (px, py, hwnd, role, name) for a book entry by ID."""
    lines = BOOK_PATH.read_text(encoding="utf-8").splitlines()
    for line in lines:
        entry = json.loads(line)
        if entry["id"] == selector:
            px = entry["px"] + entry["pw"] // 2
            py = entry["py"] + entry["ph"] // 2
            return (px, py, entry["hwnd"], entry["role"], entry["name"])
    assert False, f"id not in book: {selector}"


# ─── VALIDATION ───────────────────────────────────────────────────────────────


def validate_action(action_str: str) -> str | None:
    verb = action_str.split()[0].rstrip(":;,")
    rest = action_str[len(action_str.split()[0]):].strip()
    match verb:
        case "click" | "double_click":
            node_id = rest.split()[0]
            if not node_id.isdigit():
                return f"{verb} requires a numeric ID, got: {rest}"
            lines = BOOK_PATH.read_text(encoding="utf-8").splitlines()
            valid_ids = {json.loads(l)["id"] for l in lines}
            if node_id not in valid_ids:
                return f"ID {node_id} not in screen. Available: {sorted(valid_ids, key=int)}"
        case "type":
            m = re.match(r"(\d+)\s+(.+)", rest)
            if not m:
                return f"type requires 'ID text' format, got: {rest}"
            node_id = m.group(1)
            lines = BOOK_PATH.read_text(encoding="utf-8").splitlines()
            valid_ids = {json.loads(l)["id"] for l in lines}
            if node_id not in valid_ids:
                return f"ID {node_id} not in screen. Available: {sorted(valid_ids, key=int)}"
        case "press":
            if not rest:
                return "press requires key name(s)"
        case "done":
            return None
        case _:
            return f"unknown action: {verb}. Valid: click, double_click, type, press, done"
    return None


# ─── ACTION EXECUTION ─────────────────────────────────────────────────────────


def phase_act(action_str: str) -> tuple[str, dict]:
    """Execute action, return (result_description, interaction_entry)."""
    verb = action_str.split()[0].rstrip(":;,")
    rest = action_str[len(action_str.split()[0]):].strip()
    interaction: dict = {}
    match verb:
        case "click":
            node_id = rest.split()[0]
            px, py, hwnd, role, name = resolve_selector(node_id)
            click(hwnd, px, py)
            result = f"clicked {node_id} at ({px},{py})"
            interaction = {"action": "click", "hwnd": hwnd, "px": px, "py": py,
                           "role": role, "name": name, "element_id": node_id}
        case "double_click":
            node_id = rest.split()[0]
            px, py, hwnd, role, name = resolve_selector(node_id)
            double_click(hwnd, px, py)
            result = f"double_clicked {node_id} at ({px},{py})"
            interaction = {"action": "double_click", "hwnd": hwnd, "px": px, "py": py,
                           "role": role, "name": name, "element_id": node_id}
        case "type":
            m = re.match(r"(\d+)\s+(.+)", rest)
            assert m, f"no ID in type action: {rest}"
            node_id = m.group(1)
            text = m.group(2)
            px, py, hwnd, role, name = resolve_selector(node_id)
            click(hwnd, px, py)
            time.sleep(0.3)
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


# ─── PLANNER PHASE ────────────────────────────────────────────────────────────


def phase_planner(goal: str, backend: str, timeout: int) -> dict:
    """Call planner LLM. Returns {expand_hwnds, done_so_far, next_step}."""
    # Build planner input
    parts: list[str] = [f"GOAL: {goal}"]
    # Actor history (full, never truncated)
    if ACTOR_HISTORY_PATH.exists():
        history_text = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip()
        if history_text:
            parts.append(f"ACTOR HISTORY:\n{history_text}")
    # Verified state
    if VERIFIED_STATE_PATH.exists():
        vs_text = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip()
        if vs_text:
            parts.append(f"VERIFIED ELEMENT STATE:\n{vs_text}")
    # HWND list (from raw.txt — extract hwnd entries with depth=0 and visible+titled)
    raw_path = BASE_PATH / "raw.txt"
    if raw_path.exists():
        hwnd_lines: list[str] = []
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            if "hwnd" in obj and obj.get("depth") == 0 and obj.get("visible") and obj.get("title"):
                hwnd_lines.append(f"  hwnd={obj['hwnd']} title=\"{obj['title']}\"")
        if hwnd_lines:
            parts.append("AVAILABLE WINDOWS:\n" + "\n".join(hwnd_lines))
    user_prompt = "\n\n".join(parts)
    planner_prompt = load_prompt("planner_system_prompt.txt")
    REQUEST_PATH.write_text(
        f"SYSTEM:\n{planner_prompt}\n\nUSER:\n{user_prompt}", encoding="utf-8")
    log(f"[PLANNER REQUEST]\n{user_prompt}")
    result = subprocess.run(
        [sys.executable, str(BASE_PATH / "llm.py"), backend, "planner"],
        timeout=timeout, capture_output=True,
    )
    assert result.returncode == 0, f"planner LLM failed: {result.stderr.decode('utf-8', 'replace')[:300]}"
    raw = RESPONSE_PATH.read_text(encoding="utf-8").strip()
    log(f"[PLANNER RAW RESPONSE]\n{raw}")
    start = raw.find("{")
    end = raw.rfind("}")
    parsed = json.loads(raw[start:end + 1])
    # Normalize field names (ACP may use different keys than strict schema)
    raw_hwnds = parsed.get("expand_hwnds") or parsed.get("windows_to_expand") or []
    # Sanitize: only keep values that are integers (ACP may return strings like "Calculator")
    expand_hwnds = [int(h) for h in raw_hwnds if str(h).isdigit()]
    return {
        "expand_hwnds": expand_hwnds,
        "done_so_far": parsed.get("done_so_far") or parsed.get("reasoning") or parsed.get("progress") or "",
        "next_step": parsed.get("next_step") or parsed.get("next") or "",
    }


# ─── ACTOR PHASE ─────────────────────────────────────────────────────────────


def phase_think(goal: str, planner_output: dict, timeout: int, backend: str) -> dict:
    """Call actor LLM with screen context + planner todo."""
    context = CONTEXT_PATH.read_text(encoding="utf-8")
    parts = [f"SCREEN:\n{context}"]
    # Inject planner's todo instead of raw history
    parts.append(f"GOAL: {goal}")
    parts.append(f"PROGRESS: {planner_output['done_so_far']}")
    parts.append(f"NEXT STEP: {planner_output['next_step']}")
    user_prompt = "\n\n".join(parts)
    actor_prompt = load_prompt("actor_system_prompt.txt")
    REQUEST_PATH.write_text(f"SYSTEM:\n{actor_prompt}\n\nUSER:\n{user_prompt}", encoding="utf-8")
    log(f"[ACTOR REQUEST]\n{user_prompt}")
    result = subprocess.run(
        [sys.executable, str(BASE_PATH / "llm.py"), backend, "actor"],
        timeout=timeout, capture_output=True,
    )
    assert result.returncode == 0, f"actor LLM failed: {result.stderr.decode('utf-8', 'replace')[:300]}"
    raw = RESPONSE_PATH.read_text(encoding="utf-8").strip()
    log(f"[ACTOR RAW RESPONSE]\n{raw}")
    start = raw.find("{")
    end = raw.rfind("}")
    parsed = json.loads(raw[start:end + 1])
    # Normalize: support both "actions" (array) and legacy "action" (string)
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


# ─── COLLECTION ───────────────────────────────────────────────────────────────


def phase_collect(expand_hwnds: list[int] | None) -> None:
    cmd = [sys.executable, str(BASE_PATH / "collector.py"),
           "--timeout", "5", "--probe-step", "50", "--probe-dwell", "8"]
    if expand_hwnds:
        cmd.extend(["--expand-hwnds", ",".join(str(h) for h in expand_hwnds)])
    subprocess.run(cmd, check=True)


def phase_render() -> None:
    subprocess.run([sys.executable, str(BASE_PATH / "render.py")], check=True)


def save_lesson(goal: str, cycle: int, actor_history: str, verified_state: str) -> None:
    """Write a lesson snapshot to lessons/ directory."""
    from datetime import datetime
    LESSONS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    lesson = {
        "timestamp": timestamp,
        "goal": goal,
        "cycle": cycle,
        "actor_history": actor_history,
        "verified_state": verified_state,
    }
    path = LESSONS_DIR / f"lesson_{timestamp}.json"
    path.write_text(json.dumps(lesson, indent=2), encoding="utf-8")
    log(f"[LESSON] Saved snapshot → {path.name}")


# ─── EVOLVE PIPELINE ──────────────────────────────────────────────────────────


def evolve(args: list[str]) -> None:
    """Read accumulated lesson files, run reflection LLM, apply prompt/schema rewrites."""
    backend = args[0] if args else "lmstudio"
    log(f"[EVOLVE] Backend: {backend}")
    log("[EVOLVE] Reading accumulated lessons...")
    LESSONS_DIR.mkdir(exist_ok=True)
    lesson_files = sorted(LESSONS_DIR.glob("lesson_*.json"))
    if not lesson_files:
        # Fallback: use orphaned actor_history.jsonl (from crash or --apply run)
        if ACTOR_HISTORY_PATH.exists() and ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip():
            log("[EVOLVE] No lesson files, but found actor_history.jsonl — using it.")
            history = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip()
            vs = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip() if VERIFIED_STATE_PATH.exists() else ""
            # Extract goal from lessons.txt if available
            goal = "aggregate"
            if LESSONS_PATH.exists():
                for line in LESSONS_PATH.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        goal = json.loads(line).get("goal", goal)
            ACTOR_HISTORY_PATH.write_text(history, encoding="utf-8")
            VERIFIED_STATE_PATH.write_text(vs, encoding="utf-8")
            import autonomous_self_correct
            autonomous_self_correct._external_log = log
            analysis = autonomous_self_correct.run(goal, backend, apply=True)
            log(f"[EVOLVE] Result: {json.dumps(analysis, indent=2)}")
            log("[EVOLVE] Done. Prompts and schemas updated.")
            return
        log("[EVOLVE] No lesson files found. Nothing to do.")
        return
    log(f"[EVOLVE] Found {len(lesson_files)} lesson files.")
    # Aggregate all lessons into one context
    all_histories = []
    goal = "aggregate"
    for lf in lesson_files:
        data = json.loads(lf.read_text(encoding="utf-8"))
        goal = data.get("goal", goal)
        if data.get("actor_history"):
            all_histories.append(f"--- Run at cycle {data['cycle']} ---\n{data['actor_history']}")
    # Write aggregated history for the reflection pass
    ACTOR_HISTORY_PATH.write_text("\n".join(all_histories), encoding="utf-8")
    last_data = json.loads(lesson_files[-1].read_text(encoding="utf-8"))
    VERIFIED_STATE_PATH.write_text(last_data.get("verified_state", ""), encoding="utf-8")
    import autonomous_self_correct
    autonomous_self_correct._external_log = log
    analysis = autonomous_self_correct.run(goal, backend, apply=True)
    log(f"[EVOLVE] Result: {json.dumps(analysis, indent=2)}")
    # Archive processed lessons
    archive_dir = LESSONS_DIR / "processed"
    archive_dir.mkdir(exist_ok=True)
    for lf in lesson_files:
        lf.rename(archive_dir / lf.name)
    log(f"[EVOLVE] Archived {len(lesson_files)} lessons → lessons/processed/")
    log("[EVOLVE] Done. Prompts and schemas updated.")


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────


def main() -> None:
    time.sleep(10)
    global _run_log_file
    sys.stdout.reconfigure(encoding="utf-8")

    # Parse arguments
    args = sys.argv[1:]
    # Extract flags
    autonomous = "--autonomous" in args
    apply_flag = "--apply" in args
    self_improve = "--self-improve" in args
    evolve_flag = "--evolve" in args
    args = [a for a in args if a not in ("--autonomous", "--apply", "--self-improve", "--evolve")]

    # ─── EVOLVE MODE: separate pipeline ──────────────────────────────────────
    if evolve_flag:
        evolve(args)
        return

    goal = args[0] if len(args) > 0 else "describe what you see"
    max_cycles = int(args[1]) if len(args) > 1 else 10
    backend = args[2] if len(args) > 2 else "lmstudio"

    # Clean state for fresh run
    INTERACTION_LOG_PATH.write_text("", encoding="utf-8")
    ACTOR_HISTORY_PATH.write_text("", encoding="utf-8")
    _run_log_file = open(RUN_LOG_PATH, "w", encoding="utf-8")

    log(f"[CONFIG] goal={goal}")
    log(f"[CONFIG] max_cycles={max_cycles} backend={backend}")
    log(f"[CONFIG] autonomous={autonomous} apply={apply_flag} self_improve={self_improve}")

    expand_hwnds: list[int] | None = None
    last_cycle = 0
    try:
      for cycle in range(1, max_cycles + 1):
        last_cycle = cycle
        log(f"\n{'='*60}")
        log(f"CYCLE {cycle}")
        log(f"{'='*60}")
        # 1. Collect (with planner-expanded hwnds from previous cycle)
        phase_collect(expand_hwnds)
        # 2. Call planner (decides which windows to expand + produces todo)
        log("\n[PLANNER]")
        planner_output = phase_planner(goal, backend, 120)
        log(f"  DONE: {planner_output['done_so_far']}")
        log(f"  NEXT: {planner_output['next_step']}")
        log(f"  EXPAND: {planner_output['expand_hwnds']}")
        # 3. If planner wants additional windows, re-collect with expansions
        planner_expand = planner_output.get("expand_hwnds", [])
        if planner_expand:
            phase_collect(planner_expand)
        # 4. Render
        phase_render()
        context = CONTEXT_PATH.read_text(encoding="utf-8")
        log(f"\nSCREEN:\n{context}")
        # 4.5 Mid-run lesson capture every 2 cycles
        if autonomous and cycle > 1 and cycle % 2 == 0:
            if apply_flag:
                log(f"[SELF-IMPROVE] Cycle {cycle} checkpoint — mid-run reflection...")
                import autonomous_self_correct
                autonomous_self_correct._external_log = log
                autonomous_self_correct.run(goal, backend, apply=True)
            else:
                history = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip() if ACTOR_HISTORY_PATH.exists() else ""
                vs = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip() if VERIFIED_STATE_PATH.exists() else ""
                save_lesson(goal, cycle, history, vs)
        # 5. Call actor
        response = phase_think(goal, planner_output, 120, backend)
        log(f"\nOBSERVE: {response.get('observe', '')}")
        log(f"REASON: {response.get('reason', '')}")
        log(f"EXPECT: {response.get('expect', '')}")
        log(f"ACTIONS: {response['actions']}")
        log(f"EXTENDED_OBS: {response.get('extended_observation_needed', False)}")
        log(f"FEEDBACK: {response.get('developer_feedback', '')}")
        # 6. Check done
        if "done" in response["actions"]:
            # Trim actions up to and including done
            response["actions"] = response["actions"][:response["actions"].index("done") + 1]
        # Execute action chain
        chain_ok = True
        for i, action_str in enumerate(response["actions"]):
            if action_str == "done":
                with open(ACTOR_HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(response) + "\n")
                log("\n*** DONE ***")
                chain_ok = False  # signal outer loop to break
                break
            # Validate
            error = validate_action(action_str)
            if error:
                log(f"VALIDATION ERROR on action[{i}] '{action_str}': {error}")
                response["validation_error"] = error
                with open(ACTOR_HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(response) + "\n")
                if autonomous:
                    log("[SELF-IMPROVE] Validation error triggered mid-run reflection...")
                    import autonomous_self_correct
                    autonomous_self_correct._external_log = log
                    autonomous_self_correct.run(goal, backend, apply=apply_flag)
                chain_ok = False
                break
            # Execute
            result, interaction = phase_act(action_str)
            log(f"RESULT[{i}]: {result}")
            interaction["cycle"] = cycle
            with open(INTERACTION_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(interaction) + "\n")
            time.sleep(0.5)
        if not chain_ok and "done" in response.get("actions", []):
            break  # goal achieved
        if not chain_ok:
            expand_hwnds = planner_expand
            time.sleep(2.0)
            continue
        # Append valid actor response to history
        with open(ACTOR_HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(response) + "\n")
        # Prepare expand_hwnds for next cycle
        if response.get("extended_observation_needed"):
            expand_hwnds = planner_expand
        else:
            expand_hwnds = None
        time.sleep(2.0)
    except Exception as exc:
        log(f"\n[CRASH] Cycle {last_cycle}: {type(exc).__name__}: {exc}")
        # Always save lesson on crash so knowledge is never lost
        history = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip() if ACTOR_HISTORY_PATH.exists() else ""
        vs = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip() if VERIFIED_STATE_PATH.exists() else ""
        if history:
            save_lesson(goal, last_cycle, history, vs)
            log(f"[CRASH] Lesson saved (cycles 1-{last_cycle})")
        raise

    # ─── POST-RUN: AUTONOMOUS SELF-IMPROVEMENT ────────────────────────────────
    if autonomous or self_improve:
        if apply_flag:
            log("\n[SELF-IMPROVE] Running post-run analysis with apply...")
            import autonomous_self_correct
            autonomous_self_correct._external_log = log
            analysis = autonomous_self_correct.run(goal, backend, apply=True)
            log(f"[SELF-IMPROVE] Result: {json.dumps(analysis, indent=2)}")
        else:
            log("\n[LESSON] Saving final run snapshot (use --evolve to apply lessons later)...")
            history = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip() if ACTOR_HISTORY_PATH.exists() else ""
            vs = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip() if VERIFIED_STATE_PATH.exists() else ""
            save_lesson(goal, max_cycles, history, vs)
    else:
        log("\n[SELF-IMPROVE] Skipped (use --autonomous or --self-improve to enable)")

    _run_log_file.close()


if __name__ == "__main__":
    main()
