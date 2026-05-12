"""
Endgame-AI v7 — Autonomous Self-Correction with Detailed Backup Logging.

Called by main.py after the action loop finishes.
Reads run artifacts, calls LLM to extract lessons + prompt rewrites.
When --apply is set, overwrites prompts/*.txt with detailed backups.
"""
from __future__ import annotations
import json
import pathlib
import shutil
import subprocess
import sys
from datetime import datetime

BASE_PATH = pathlib.Path(__file__).parent
PROMPTS_DIR = BASE_PATH / "prompts"
BACKUP_DIR = BASE_PATH / "prompt_backups"
LESSONS_PATH = BASE_PATH / "lessons.txt"
LOG_FILE = BASE_PATH / "v7_autonomous_correction.log"
ACTOR_HISTORY_PATH = BASE_PATH / "actor_history.jsonl"
INTERACTION_LOG_PATH = BASE_PATH / "interaction_log.jsonl"
VERIFIED_STATE_PATH = BASE_PATH / "verified_state.txt"
REQUEST_PATH = BASE_PATH / "request.txt"
RESPONSE_PATH = BASE_PATH / "response.txt"


_external_log = None


def log(msg: str):
    """Log to console, v7_autonomous_correction.log, and external logger if set."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if _external_log:
        _external_log(msg)


def load_self_reflect_prompt() -> str:
    path = PROMPTS_DIR / "self_reflection_system_prompt.txt"
    if not path.exists():
        path = PROMPTS_DIR / "original" / "self_reflection_system_prompt.txt"
    return path.read_text(encoding="utf-8").strip()


def create_detailed_backup(prompt_name: str, old_content: str, new_content: str) -> pathlib.Path:
    """Create a detailed backup with metadata in prompt_backups/YYYYMMDD_HHMMSS/."""
    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_subdir = BACKUP_DIR / timestamp
    backup_subdir.mkdir(exist_ok=True)

    # Save old version
    (backup_subdir / f"{prompt_name}_OLD.txt").write_text(old_content, encoding="utf-8")
    # Save new version
    (backup_subdir / f"{prompt_name}_NEW.txt").write_text(new_content, encoding="utf-8")
    # Create metadata
    meta = {
        "timestamp": timestamp,
        "prompt_name": prompt_name,
        "old_length": len(old_content),
        "new_length": len(new_content),
        "changed": old_content.strip() != new_content.strip(),
        "backup_location": str(backup_subdir),
    }
    (backup_subdir / "backup_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log(f"  → Detailed backup created for '{prompt_name}' in {backup_subdir}")
    return backup_subdir


def extract_lessons(goal: str, backend: str, timeout: int = 120) -> dict:
    """Call LLM to analyze the run and extract lessons + prompt rewrites."""
    parts = [f"GOAL: {goal}"]

    if ACTOR_HISTORY_PATH.exists():
        history = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip()
        if history:
            parts.append(f"ACTOR HISTORY:\n{history}")

    if INTERACTION_LOG_PATH.exists():
        interactions = INTERACTION_LOG_PATH.read_text(encoding="utf-8").strip()
        if interactions:
            parts.append(f"INTERACTION LOG:\n{interactions}")

    if VERIFIED_STATE_PATH.exists():
        vs = VERIFIED_STATE_PATH.read_text(encoding="utf-8").strip()
        if vs:
            parts.append(f"FINAL VERIFIED STATE:\n{vs}")

    # Include current prompts so the model knows what it's improving
    actor_path = PROMPTS_DIR / "actor_system_prompt.txt"
    planner_path = PROMPTS_DIR / "planner_system_prompt.txt"
    if actor_path.exists():
        parts.append(f"CURRENT ACTOR PROMPT ({len(actor_path.read_text(encoding='utf-8'))} chars — rewrite must not exceed this):\n{actor_path.read_text(encoding='utf-8').strip()}")
    if planner_path.exists():
        parts.append(f"CURRENT PLANNER PROMPT ({len(planner_path.read_text(encoding='utf-8'))} chars — rewrite must not exceed this):\n{planner_path.read_text(encoding='utf-8').strip()}")

    # Feed current schemas so reflection can propose evolution
    actor_schema_path = PROMPTS_DIR / "actor_schema.json"
    planner_schema_path = PROMPTS_DIR / "planner_schema.json"
    if actor_schema_path.exists():
        parts.append(f"CURRENT ACTOR SCHEMA:\n{actor_schema_path.read_text(encoding='utf-8').strip()}")
    if planner_schema_path.exists():
        parts.append(f"CURRENT PLANNER SCHEMA:\n{planner_schema_path.read_text(encoding='utf-8').strip()}")

    # Feed accumulated lessons as context for the rewrite decision
    if LESSONS_PATH.exists():
        lessons = LESSONS_PATH.read_text(encoding="utf-8").strip()
        if lessons:
            parts.append(f"LESSONS FROM ALL PREVIOUS RUNS (use to inform rewrite priorities):\n{lessons}")

    # Determine outcome
    if ACTOR_HISTORY_PATH.exists():
        lines = ACTOR_HISTORY_PATH.read_text(encoding="utf-8").strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
            actions = last.get("actions") or [last.get("action", "")]
            if "done" in actions:
                parts.append("RUN OUTCOME: Actor declared DONE (goal achieved).")
            elif last.get("validation_error"):
                parts.append(f"RUN OUTCOME: Last action had validation error: {last['validation_error']}")
            else:
                parts.append("RUN OUTCOME: Max cycles reached without completing goal.")

    user_prompt = "\n\n".join(parts)
    system_prompt = load_self_reflect_prompt()
    REQUEST_PATH.write_text(f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(BASE_PATH / "llm.py"), backend, "reflect"],
        timeout=timeout, capture_output=True,
    )
    if result.returncode != 0:
        log("ERROR: LLM call failed during self-reflection")
        return {"outcome": "error", "lessons": ["LLM call failed during self-reflection"],
                "actor_prompt_rewrite": "", "planner_prompt_rewrite": ""}

    raw = RESPONSE_PATH.read_text(encoding="utf-8").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        log("ERROR: Could not parse self-reflection response")
        return {"outcome": "error", "lessons": ["Could not parse self-reflection response"],
                "actor_prompt_rewrite": "", "planner_prompt_rewrite": ""}
    return json.loads(raw[start:end + 1])


def apply_prompt_rewrites(analysis: dict) -> list[str]:
    """Overwrite prompt files with detailed backups. All-or-nothing: if any rewrite is rejected, none are applied."""
    pending = []

    prompt_map = {
        "actor_prompt_rewrite": "actor_system_prompt.txt",
        "planner_prompt_rewrite": "planner_system_prompt.txt",
    }

    for key, filename in prompt_map.items():
        new_content = analysis.get(key, "").strip()
        if not new_content:
            continue

        target = PROMPTS_DIR / filename
        old_content = target.read_text(encoding="utf-8") if target.exists() else ""

        # Fixed-size gate: reject rewrites that exceed 110% of current size
        if old_content and len(new_content) > len(old_content) * 1.1:
            log(f"  [REJECTED] {filename}: rewrite too large ({len(new_content)} > {int(len(old_content)*1.1)} limit).")
            return []  # all-or-nothing: abort entire evolution

        pending.append((target, filename, old_content, new_content))

    # Apply schema rewrites
    schema_map = {
        "actor_schema_rewrite": "actor_schema.json",
        "planner_schema_rewrite": "planner_schema.json",
    }

    for key, filename in schema_map.items():
        new_content = analysis.get(key, "").strip()
        if not new_content:
            continue
        # Validate it's parseable JSON
        try:
            parsed = json.loads(new_content)
        except json.JSONDecodeError as e:
            log(f"  [REJECTED] {filename}: invalid JSON ({e}).")
            return []  # all-or-nothing

        # Safety: ensure critical fields still exist
        props = parsed.get("json_schema", {}).get("schema", {}).get("properties", {})
        if "actor" in filename and "actions" not in props:
            log(f"  [REJECTED] {filename}: missing required 'actions' field.")
            return []  # all-or-nothing
        if "planner" in filename and "expand_hwnds" not in props:
            log(f"  [REJECTED] {filename}: missing required 'expand_hwnds' field.")
            return []  # all-or-nothing

        target = PROMPTS_DIR / filename
        old_content = target.read_text(encoding="utf-8") if target.exists() else ""
        pending.append((target, filename, old_content, new_content))

    if not pending:
        return []

    # All validations passed — commit all writes atomically
    changed = []
    for target, filename, old_content, new_content in pending:
        create_detailed_backup(filename.replace(".txt", "").replace(".json", ""), old_content, new_content)
        target.write_text(new_content, encoding="utf-8")
        log(f"  [UPDATED] {filename} → {len(new_content)} characters (was {len(old_content)})")
        changed.append(filename)

    return changed


def append_lessons(analysis: dict, goal: str) -> None:
    """Append extracted lessons to lessons.txt (persistent across runs)."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "timestamp": timestamp,
        "goal": goal,
        "outcome": analysis.get("outcome", "unknown"),
        "cycles_used": analysis.get("cycles_used", 0),
        "lessons": analysis.get("lessons", []),
        "patterns_that_worked": analysis.get("patterns_that_worked", []),
        "patterns_that_failed": analysis.get("patterns_that_failed", []),
    }
    with open(LESSONS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def generate_report(analysis: dict, goal: str, changed: list[str]) -> pathlib.Path:
    """Write self_improvement_report_YYYYMMDD_HHMMSS.txt."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = BASE_PATH / f"self_improvement_report_{timestamp}.txt"
    lines = [
        f"ENDGAME-AI v7 SELF-IMPROVEMENT REPORT",
        f"Generated: {datetime.now().isoformat()}",
        f"Goal: {goal}",
        f"Outcome: {analysis.get('outcome', 'unknown')}",
        f"Cycles used: {analysis.get('cycles_used', 'unknown')}",
        "",
        "LESSONS LEARNED:",
        *[f"  • {l}" for l in analysis.get("lessons", [])],
        "",
        "PATTERNS THAT WORKED:",
        *[f"  ✓ {p}" for p in analysis.get("patterns_that_worked", [])],
        "",
        "PATTERNS THAT FAILED:",
        *[f"  ✗ {p}" for p in analysis.get("patterns_that_failed", [])],
        "",
        f"PROMPTS MODIFIED: {changed if changed else 'None'}",
        f"Backups: {BACKUP_DIR}",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def run(goal: str, backend: str, apply: bool = False) -> dict:
    """Main entry point. Called by main.py after the action loop."""
    log("=" * 70)
    log(">>> ENDGAME-AI v7 AUTONOMOUS SELF-CORRECTION <<<")
    log("=" * 70)
    log(f"Goal: {goal}")
    log(f"Backend: {backend}")
    log(f"Apply: {apply}")

    log("[v7] Analyzing run and extracting lessons...")
    analysis = extract_lessons(goal, backend)
    log(f"Outcome: {analysis.get('outcome', 'unknown')}")
    log(f"Lessons: {analysis.get('lessons', [])}")

    # Always save lessons first
    append_lessons(analysis, goal)
    log(f"Lessons appended to {LESSONS_PATH}")

    # Apply prompt rewrites with detailed backups
    changed = []
    evolution_ok = False
    if apply:
        log("\n=== STARTING EVOLUTION (ALL-OR-NOTHING) ===")
        # Check if reflection proposed any rewrites at all
        has_proposals = bool(
            analysis.get("actor_prompt_rewrite", "").strip() or
            analysis.get("planner_prompt_rewrite", "").strip() or
            analysis.get("actor_schema_rewrite", "").strip() or
            analysis.get("planner_schema_rewrite", "").strip()
        )
        if has_proposals:
            changed = apply_prompt_rewrites(analysis)
            if changed:
                log(f"=== EVOLUTION SUCCEEDED: {changed} ===")
                evolution_ok = True
            else:
                log("=== EVOLUTION FAILED (rejected by gates) — lessons preserved ===")
        else:
            log("=== NO EVOLUTION NEEDED (prompts already optimal) ===")
            evolution_ok = True  # nothing to change = knowledge already absorbed

        if evolution_ok:
            # Knowledge absorbed into prompts — archive and clear lessons
            archive_path = BASE_PATH / "lessons_archive.txt"
            if LESSONS_PATH.exists() and LESSONS_PATH.read_text(encoding="utf-8").strip():
                archive_path.open("a", encoding="utf-8").write(
                    LESSONS_PATH.read_text(encoding="utf-8"))
                LESSONS_PATH.write_text("", encoding="utf-8")
                log(f"[EVOLVE] lessons.txt archived and cleared")
        log("=== EVOLUTION COMPLETE ===")
    else:
        has_rewrites = bool(analysis.get("actor_prompt_rewrite") or analysis.get("planner_prompt_rewrite"))
        if has_rewrites:
            log("Prompt rewrites proposed but NOT applied (use --apply to enable).")

    # Generate report file
    report_path = generate_report(analysis, goal, changed)
    log(f"Report written: {report_path}")

    log("=" * 70)
    log("[v7] Autonomous self-correction finished.")
    log(f"Backup logs: {BACKUP_DIR}")
    log(f"Full log: {LOG_FILE}")
    log("=" * 70)

    return analysis


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("goal", type=str)
    parser.add_argument("--backend", default="lmstudio")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run(args.goal, args.backend, args.apply)
