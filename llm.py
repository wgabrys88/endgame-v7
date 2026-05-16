from __future__ import annotations
import json
import os
import pathlib
import subprocess
import tempfile

from log import log, pretty

BASE_PATH = pathlib.Path(__file__).parent
PROMPTS_DIR = BASE_PATH / "prompts"
LMS_HOSTS = ["http://localhost:1234", "http://192.168.16.31:1234"]
SCHEMA_MAP = {"actor": "actor_schema.json", "planner": "planner_schema.json", "reflect": "reflect_schema.json"}

TIMEOUT_HOST_CURL = 3
TIMEOUT_HOST_CHECK = 50
TIMEOUT_MODEL_LIST = 100
TIMEOUT_INFERENCE_CURL = 9000
TIMEOUT_INFERENCE_PROC = 10000
TIMEOUT_ACP_PROMPT = 300.0

_cached_host: str | None = None
_cached_model: str | None = None


def load_schema(role: str) -> dict:
    return json.loads((PROMPTS_DIR / SCHEMA_MAP[role]).read_text(encoding="utf-8"))


def find_lms_host() -> str:
    global _cached_host
    if _cached_host:
        return _cached_host
    for host in LMS_HOSTS:
        r = subprocess.run(
            ["curl.exe", "-s", "--max-time", str(TIMEOUT_HOST_CURL), f"{host}/v1/models"],
            capture_output=True, timeout=TIMEOUT_HOST_CHECK)
        if r.returncode == 0 and r.stdout.strip():
            _cached_host = host
            return host
    assert False, "no LM Studio host reachable"


def get_model(host: str) -> str:
    global _cached_model
    if _cached_model:
        return _cached_model
    r = subprocess.run(["curl.exe", "-s", f"{host}/v1/models"], capture_output=True, timeout=TIMEOUT_MODEL_LIST)
    _cached_model = json.loads(r.stdout)["data"][0]["id"]
    return _cached_model


def build_request(system: str, user: str, schema: dict, **hyperparams) -> dict:
    defaults = {
        "temperature": 0.22,
        "top_p": 0.92,
        "top_k": 26,
        "max_tokens": 6500,
        "repeat_penalty": 1.13,
        "frequency_penalty": 0.07,
        "presence_penalty": 0.04,
        "seed": None,
        "stream": False,
    }
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
    tmp.write(json.dumps(body, ensure_ascii=False))
    tmp.close()
    proc = subprocess.run(
        ["curl.exe", "-sN", "-X", "POST", f"{host}/v1/chat/completions",
         "-H", "Content-Type: application/json", "-d", f"@{tmp.name}",
         "--max-time", str(TIMEOUT_INFERENCE_CURL)],
        capture_output=True, timeout=TIMEOUT_INFERENCE_PROC)
    os.unlink(tmp.name)
    assert proc.returncode == 0, f"curl failed: {proc.stderr}"
    raw = proc.stdout.decode("utf-8")
    assert raw.strip(), "empty LLM response"
    return json.loads(raw)["choices"][0]["message"]["content"]


def call_acp(body: dict) -> str:
    from acp_client import prompt_once
    return prompt_once(json.dumps(body, ensure_ascii=False), timeout=TIMEOUT_ACP_PROMPT)


_max_request_tokens: int | None = None


def set_max_request_tokens(limit: int | None) -> None:
    global _max_request_tokens
    _max_request_tokens = limit


def call_backend(system: str, user: str, backend: str, role: str) -> str:
    schema = load_schema(role)
    body = build_request(system, user, schema)
    body_json = json.dumps(body, ensure_ascii=False)
    chars = len(body_json)
    tokens_est = chars // 4
    # Token guard: fail hard if request exceeds developer-set limit
    if _max_request_tokens is not None:
        assert tokens_est <= _max_request_tokens, (
            f"[{role.upper()}] request too large: ~{tokens_est} tokens "
            f"(limit: {_max_request_tokens}). Chars: {chars}"
        )
    log(f"[{role.upper()} RAW REQUEST ~{tokens_est}tok]\n{pretty(json.dumps(body, indent=2, ensure_ascii=False))}")
    match backend:
        case "lmstudio": result = call_lmstudio(body)
        case "acp": result = call_acp(body)
        case _: assert False, f"unknown backend: {backend}"
    return result
