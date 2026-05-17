from __future__ import annotations
import json
import pathlib
import re

from llm import call_backend
from log import log, log_file, pretty, read_current_run, read_last_n, read_since_last_lesson

BASE_PATH = pathlib.Path(__file__).parent
PROMPTS_DIR = BASE_PATH / "prompts"
VERIFIED_STATE_PATH = BASE_PATH / "verified_state.txt"
INTERACTION_LOG_PATH = BASE_PATH / "interaction_log.jsonl"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


def call_llm(system: str, user: str, backend: str, role: str, retries: int = 2) -> dict:
    last_err: Exception | None = None
    for attempt in range(1, retries + 2):
        raw = call_backend(system, user, backend, role)
        tokens_est = len(raw) // 4
        log(f"[{role.upper()} RESPONSE ~{tokens_est}tok]")
        log(pretty(raw))
        log_file(raw)
        try:
            return _extract_json(raw, role)
        except (AssertionError, json.JSONDecodeError) as e:
            last_err = e
            log(f"[{role.upper()}] JSON parse failed (attempt {attempt}): {e}")
    raise AssertionError(f"{role} JSON parse failed after {retries + 1} attempts") from last_err


def _extract_json(raw: str, role: str) -> dict:
    fence_match = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```", raw, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    assert start != -1, f"Could not parse {role} response as JSON: no '{{' found"
    depth = 0
    in_string = False
    escape_next = False
    end = -1
    for i in range(start, len(raw)):
        ch = raw[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert end != -1, f"Could not parse {role} response as JSON: no matching '}}' found"
    candidate = raw[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        last_end = raw.rfind("}")
        if last_end != end:
            try:
                return json.loads(raw[start:last_end + 1])
            except json.JSONDecodeError:
                pass
        raise AssertionError(f"Could not parse {role} response as JSON: {candidate[:500]}")


# --- Action description helper (natural language, no IDs/HWNDs) ---

def _describe_action(obj: dict) -> str:
    act = obj.get("action", "")
    role = obj.get("role", "")
    name = obj.get("name", "")
    element = f"{role} '{name}'" if name else role if role else "element"
    match act:
        case "click":
            return f"clicked {element}"
        case "double_click":
            return f"double-clicked {element}"
        case "type":
            typed = obj.get("typed", "")
            short = typed[:150] + "..." if len(typed) > 150 else typed
            return f"typed \"{short}\" into {element}"
        case "replace":
            typed = obj.get("typed", "")
            short = typed[:150] + "..." if len(typed) > 150 else typed
            return f"replaced {element} with \"{short}\""
        case "press":
            return f"pressed {obj.get('keys', '')}"
        case _:
            return act


# --- Data expansion resolver ---

def resolve_expansion(ref: str, range_pct: list[int], book: dict[str, dict] | None = None) -> str:
    """Resolve a data expansion request. Returns the sliced content."""
    data = _fetch_data(ref, book)
    if not data:
        return f"[{ref}: no data available]"
    start = len(data) * range_pct[0] // 100
    end = len(data) * range_pct[1] // 100
    sliced = data[start:end]
    header = f"[{ref} ({range_pct[0]}-{range_pct[1]}%, {len(sliced)} chars)]"
    return f"{header}\n{sliced}"


def _fetch_data(ref: str, book: dict[str, dict] | None = None) -> str:
    """Route a ref to its data source."""
    # cycle_N_typed → full typed text from cycle N
    m = re.match(r"cycle_(\d+)_typed", ref)
    if m:
        cycle_num = int(m.group(1))
        return _get_cycle_typed(cycle_num)

    # cycle_N_actor → full actor response from cycle N
    m = re.match(r"cycle_(\d+)_actor", ref)
    if m:
        cycle_num = int(m.group(1))
        return _get_cycle_actor(cycle_num)

    # element_ID_value or element_ID → value of element from current screen book
    m = re.match(r"element_(\d+)(?:_value)?", ref)
    if m and book:
        eid = m.group(1)
        entry = book.get(eid)
        if entry:
            return entry.get("value", "") or f"[element {eid} has no text value]"
        return f"[element {eid} not found in current screen]"

    # clipboard → current clipboard text (Windows API)
    if ref == "clipboard":
        return _get_clipboard()

    # run_history → all actions from current run as natural language
    if ref == "run_history":
        return _get_run_history()

    return f"[unknown ref: {ref}]"


def _get_cycle_typed(cycle_num: int) -> str:
    """Get full typed/replaced text from a specific cycle."""
    entries = read_current_run("ACTION")
    for raw in entries:
        try:
            obj = json.loads(raw)
            if obj.get("cycle") == cycle_num and obj.get("typed"):
                return obj["typed"]
        except (json.JSONDecodeError, AttributeError):
            pass
    return ""


def _get_cycle_actor(cycle_num: int) -> str:
    """Get full actor response from a specific cycle."""
    entries = read_current_run("ACTOR")
    if 0 < cycle_num <= len(entries):
        return entries[cycle_num - 1]
    return ""


def _get_clipboard() -> str:
    import ctypes
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalSize.argtypes = [ctypes.c_void_p]
    k32.GlobalSize.restype = ctypes.c_size_t
    u32.GetClipboardData.argtypes = [ctypes.c_uint]
    u32.GetClipboardData.restype = ctypes.c_void_p
    if not u32.OpenClipboard(0):
        return "[clipboard: could not open]"
    handle = u32.GetClipboardData(13)
    if not handle:
        u32.CloseClipboard()
        return "[clipboard: empty]"
    ptr = k32.GlobalLock(handle)
    if not ptr:
        u32.CloseClipboard()
        return "[clipboard: lock failed]"
    text = ctypes.c_wchar_p(ptr).value or ""
    k32.GlobalUnlock(handle)
    u32.CloseClipboard()
    return text


def _get_run_history() -> str:
    """Get all actions from current run as natural language."""
    entries = read_current_run("ACTION")
    if not entries:
        return ""
    lines: list[str] = []
    for raw in entries:
        try:
            obj = json.loads(raw)
            c = obj.get("cycle", 0)
            lines.append(f"  cycle {c}: {_describe_action(obj)}")
        except (json.JSONDecodeError, AttributeError):
            pass
    return "\n".join(lines)


def section_expanded_data(expansions: list[str]) -> str:
    """Format resolved expansions for injection into prompt."""
    if not expansions:
        return ""
    return "EXPANDED DATA:\n" + "\n---\n".join(expansions)


# --- Sections ---

def section_goal(goal: str) -> str:
    return f"GOAL: {goal}"


def section_screen(context_text: str) -> str:
    return f"SCREEN:\n{context_text}"


def section_actor_history() -> str:
    """Natural language history from ACTION log — no element IDs, no HWNDs."""
    action_entries = read_current_run("ACTION")
    actor_entries = read_current_run("ACTOR")
    if not action_entries and not actor_entries:
        return ""

    # Group actions by cycle
    cycles: dict[int, list[str]] = {}
    for raw in action_entries:
        try:
            obj = json.loads(raw)
            c = obj.get("cycle", 0)
            cycles.setdefault(c, []).append(_describe_action(obj))
        except (json.JSONDecodeError, AttributeError):
            pass

    # Detect wait cycles (actor entries with empty actions)
    wait_cycles: set[int] = set()
    cycle_counter = 0
    for raw in actor_entries:
        try:
            obj = json.loads(raw)
            cycle_counter += 1
            if not obj.get("actions"):
                wait_cycles.add(cycle_counter)
        except (json.JSONDecodeError, AttributeError):
            cycle_counter += 1

    # Build lines for all known cycles
    all_cycle_nums = sorted(set(cycles.keys()) | wait_cycles)
    lines: list[str] = []
    for c in all_cycle_nums:
        if c in cycles:
            lines.append(f"  cycle {c}: {', then '.join(cycles[c])}")
        else:
            lines.append(f"  cycle {c}: waited (observing)")

    # Return last 8
    return "HISTORY:\n" + "\n".join(lines[-8:]) if lines else ""


def section_developer_feedback() -> str:
    entries = read_current_run("ACTOR")
    if not entries:
        return ""
    feedbacks: list[str] = []
    for raw in entries[-4:]:
        try:
            fb = json.loads(raw).get("developer_feedback", "").strip()
            if fb:
                feedbacks.append(fb)
        except (json.JSONDecodeError, AttributeError):
            pass
    if not feedbacks:
        return ""
    return "ACTOR FEEDBACK:\n" + "\n".join(f"- {fb}" for fb in feedbacks)


def section_verified_state() -> str:
    """Natural language verified state — no HWNDs, no pixel coords."""
    if not VERIFIED_STATE_PATH.exists():
        return ""
    text = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
            role = obj.get("original_role", "")
            name = obj.get("original_name", "")
            orig = f"{role} '{name}'" if name else role if role else "element"
            status = obj.get("status", "")
            wnd = obj.get("wnd_title", "")
            if status == "OK":
                lines.append(f"  {orig} → OK (unchanged)")
            elif status == "ELEMENT_CHANGED":
                cur = obj.get("current_name", "")
                extra = f" (now in \"{wnd}\")" if wnd else ""
                lines.append(f"  {orig} → CHANGED to '{cur}'{extra}")
            elif status == "NOT_FOUND":
                lines.append(f"  {orig} → NOT FOUND (gone)")
            else:
                lines.append(f"  {orig} → {status}")
        except (json.JSONDecodeError, AttributeError):
            lines.append(f"  {line}")
    return "VERIFIED STATE (may be stale — trust SCREEN if contradicted):\n" + "\n".join(lines)


def section_available_windows(raw_lines: list[str], hwnd_map: dict | None = None) -> str:
    windows: list[tuple[int, str]] = []
    seen_hwnds: set[int] = set()
    z_order: dict[int, int] = {}
    uia_hwnds: set[int] = set()
    for line in raw_lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, AttributeError):
            continue
        if "z_order" in obj:
            for entry in obj["z_order"]:
                z_order[entry["hwnd"]] = entry["z"]
        if "wnd_name" in obj and obj.get("wnd_hwnd") and obj.get("wnd_name"):
            hwnd = obj["wnd_hwnd"]
            uia_hwnds.add(hwnd)
            if hwnd not in seen_hwnds:
                windows.append((hwnd, obj["wnd_name"]))
                seen_hwnds.add(hwnd)
    for line in raw_lines:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, AttributeError):
            continue
        if "hwnd" in obj and obj.get("depth") == 0 and obj.get("visible") and obj.get("title"):
            hwnd = obj["hwnd"]
            if hwnd not in seen_hwnds and hwnd in z_order:
                windows.append((hwnd, obj["title"]))
                seen_hwnds.add(hwnd)
    if not windows:
        return ""
    windows.sort(key=lambda w: z_order.get(w[0], 999))
    lines: list[str] = []
    for i, (hwnd, title) in enumerate(windows, 1):
        label = f"W{i}"
        lines.append(f"  {label}: \"{title}\"")
        if hwnd_map is not None:
            hwnd_map[label] = hwnd
    return "WINDOWS:\n" + "\n".join(lines)


def section_progress(planner_output: dict) -> str:
    return f"PROGRESS: {planner_output['done_so_far']}"


def section_next_step(planner_output: dict) -> str:
    return f"NEXT STEP: {planner_output['next_step']}"


def section_budget(cycle: int, max_cycles: int) -> str:
    remaining = max_cycles - cycle
    return f"BUDGET: cycle {cycle}/{max_cycles}, remaining {remaining}"


def section_current_prompts() -> str:
    parts: list[str] = []
    for name, label in [("actor_system_prompt.txt", "ACTOR"), ("planner_system_prompt.txt", "PLANNER")]:
        path = PROMPTS_DIR / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            parts.append(f"CURRENT {label} PROMPT ({len(text)} chars):\n{text.strip()}")
    return "\n\n".join(parts)


def section_current_schemas() -> str:
    parts = [
        f"CURRENT {name.upper().replace('.JSON', '')}:\n{(PROMPTS_DIR / name).read_text(encoding='utf-8').strip()}"
        for name in ("actor_schema.json", "planner_schema.json")
        if (PROMPTS_DIR / name).exists()
    ]
    return "\n\n".join(parts)


def section_lessons(n: int) -> str:
    entries = read_last_n("LESSON", n)
    return f"LESSONS FROM PREVIOUS RUNS:\n" + "\n".join(entries) if entries else ""


def section_run_outcome() -> str:
    entries = read_current_run("ACTOR")
    if not entries:
        return ""
    last = json.loads(entries[-1])
    actions = last.get("actions") or []
    if "done" in actions:
        return "RUN OUTCOME: Actor declared DONE (goal achieved)."
    if last.get("validation_error"):
        return f"RUN OUTCOME: Last action had validation error: {last['validation_error']}"
    return "RUN OUTCOME: Max cycles reached without completing goal."


def section_full_run_log() -> str:
    """Run log for the reflector — only entries since last LESSON boundary."""
    planner_entries = read_since_last_lesson("PLANNER")
    actor_entries = read_since_last_lesson("ACTOR")
    action_entries = read_since_last_lesson("ACTION")
    if not actor_entries and not action_entries:
        return ""
    lines: list[str] = []
    action_idx = 0
    for i in range(max(len(actor_entries), len(planner_entries))):
        cycle = i + 1
        # Planner decision
        if i < len(planner_entries):
            lines.append(f"[CYCLE {cycle} PLANNER] {planner_entries[i]}")
        # Actions for this cycle
        while action_idx < len(action_entries):
            try:
                act = json.loads(action_entries[action_idx])
                if act.get("cycle", 0) > cycle:
                    break
                lines.append(f"[CYCLE {act['cycle']} ACTION] {action_entries[action_idx]}")
                action_idx += 1
            except (json.JSONDecodeError, AttributeError):
                action_idx += 1
        # Actor response
        if i < len(actor_entries):
            lines.append(f"[CYCLE {cycle} ACTOR] {actor_entries[i]}")
    return "FULL RUN LOG:\n" + "\n".join(lines)


def assemble_prompt(*sections: str) -> str:
    return "\n\n".join(s for s in sections if s)
