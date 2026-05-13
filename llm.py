from __future__ import annotations
import json
import os
import pathlib
import subprocess
import tempfile

from log import log

BASE_PATH = pathlib.Path(__file__).parent
PROMPTS_DIR = BASE_PATH / "prompts"
LMS_HOSTS = ["http://localhost:1234", "http://192.168.16.31:1234"]
SCHEMA_MAP = {"actor": "actor_schema.json", "planner": "planner_schema.json", "reflect": "reflect_schema.json"}


def load_schema(role: str) -> dict:
    return json.loads((PROMPTS_DIR / SCHEMA_MAP[role]).read_text(encoding="utf-8"))


def find_lms_host() -> str:
    for host in LMS_HOSTS:
        r = subprocess.run(["curl.exe", "-s", "--max-time", "3", f"{host}/v1/models"],
                           capture_output=True, timeout=50)
        if r.returncode == 0 and r.stdout.strip():
            return host
    assert False, "no LM Studio host reachable"


def get_model(host: str) -> str:
    r = subprocess.run(["curl.exe", "-s", f"{host}/v1/models"], capture_output=True, timeout=100)
    return json.loads(r.stdout)["data"][0]["id"]


def build_request(
    system: str,
    user: str,
    schema: dict,
    **hyperparams
) -> dict:
    """Build request body for LM Studio with hyperparameters optimized
    for Q6 quantized models in structured/agentic workflows
    (planner + actor + reflector with strict JSON schemas).
    """
    # === Q6 QUANTIZED MODEL DEFAULTS (high reliability mode) ===
    defaults = {
        "temperature": 0.22,        # Very low for deterministic JSON adherence
        "top_p": 0.92,              # Slightly tighter than 0.95
        "top_k": 26,                # Strong limit on creativity (excellent for Q6)
        "max_tokens": 6500,         # Generous headroom for reasoning + JSON
        "repeat_penalty": 1.13,     # Very effective on quantized models to kill loops
        "frequency_penalty": 0.07,  # Light penalty on repetition
        "presence_penalty": 0.04,   # Very light
        "seed": None,               # Set to int (e.g. 42) for reproducible runs
        "stream": False,
    }

    # Allow runtime overrides (e.g. build_request(..., temperature=0.1, top_k=20))
    defaults.update({k: v for k, v in hyperparams.items() if v is not None})

    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": schema,
        **{k: v for k, v in defaults.items() if v is not None},
    }


def call_lmstudio(body: dict) -> str:
    host = find_lms_host()
    body["model"] = get_model(host)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    tmp.write(json.dumps(body))
    tmp.close()
    proc = subprocess.run(
        ["curl.exe", "-sN", "-X", "POST", f"{host}/v1/chat/completions",
         "-H", "Content-Type: application/json", "-d", f"@{tmp.name}", "--max-time", "9000"],
        capture_output=True, timeout=10000,
    )
    os.unlink(tmp.name)
    assert proc.returncode == 0, f"curl failed: {proc.stderr}"
    raw = proc.stdout.decode("utf-8")
    assert raw.strip(), "empty LLM response"
    return json.loads(raw)["choices"][0]["message"]["content"]


def call_acp(body: dict) -> str:
    from acp_client import prompt_once
    return prompt_once(json.dumps(body), timeout=120.0)


def call_backend(system: str, user: str, backend: str, role: str) -> str:
    schema = load_schema(role)
    body = build_request(system, user, schema)
    log(f"[{role.upper()} RAW REQUEST]\n{json.dumps(body, indent=2)}")
    match backend:
        case "lmstudio": return call_lmstudio(body)
        case "acp": return call_acp(body)
        case _: assert False, f"unknown backend: {backend}"
