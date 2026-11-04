"""LLM gateway: config-driven routing, guardrails, SQLite observability.

Run (mock path, zero API keys):
    uvicorn gateway:app --host 127.0.0.1 --port 8000

Endpoints:
    GET  /health          liveness + default model
    GET  /prompts         versioned prompt registry (names + versions)
    POST /v1/chat         route a prompt to a model, guarded and logged
    GET  /metrics         aggregate latency / tokens / cost / counts

Token counts are a whitespace approximation, documented as such: they exist
so cost estimation and observability can be demoed offline, not as a
tokenizer. Cost = tokens / 1000 * configured price.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from guardrails import find_blocked, redact_pii, truncate
from providers import ProviderError, get_provider

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("GATEWAY_DB", BASE_DIR / "gateway.db"))


def _load_yaml(name: str) -> dict:
    with open(BASE_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CONFIG = _load_yaml("config.yaml")
PROMPTS = _load_yaml("prompts.yaml").get("prompts", [])

ROUTING = CONFIG.get("routing", {})
MODELS = ROUTING.get("models", {})
DEFAULT_MODEL = ROUTING.get("default_model", "mock-llm-v1")
GUARD = CONFIG.get("guardrails", {})
MAX_PROMPT_CHARS = int(GUARD.get("max_prompt_chars", 4000))
MAX_COMPLETION_CHARS = int(GUARD.get("max_completion_chars", 2000))
BLOCKED_WORDS = list(GUARD.get("blocked_words", []))


# ---------------------------------------------------------------- store
def init_db(path: Path = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                latency_ms REAL NOT NULL,
                cost_usd REAL NOT NULL,
                prompt_name TEXT,
                prompt_version INTEGER,
                guardrail_flags TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'ok'
            )
            """
        )


def log_request(row: dict, path: Path = DB_PATH) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO requests
                (ts, model, provider, prompt_tokens, completion_tokens,
                 latency_ms, cost_usd, prompt_name, prompt_version,
                 guardrail_flags, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["ts"],
                row["model"],
                row["provider"],
                row["prompt_tokens"],
                row["completion_tokens"],
                row["latency_ms"],
                row["cost_usd"],
                row.get("prompt_name"),
                row.get("prompt_version"),
                json.dumps(row.get("guardrail_flags", [])),
                row.get("status", "ok"),
            ),
        )


def aggregate_metrics(path: Path = DB_PATH) -> dict:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM requests").fetchall()
    total = len(rows)
    total_cost = sum(r["cost_usd"] for r in rows)
    total_prompt = sum(r["prompt_tokens"] for r in rows)
    total_completion = sum(r["completion_tokens"] for r in rows)
    avg_latency = (
        sum(r["latency_ms"] for r in rows) / total if total else 0.0
    )
    per_model: dict[str, dict] = {}
    guardrail_triggers = 0
    for r in rows:
        bucket = per_model.setdefault(
            r["model"], {"requests": 0, "cost_usd": 0.0, "avg_latency_ms": 0.0}
        )
        bucket["requests"] += 1
        bucket["cost_usd"] += r["cost_usd"]
        bucket["avg_latency_ms"] += r["latency_ms"]
        flags = json.loads(r["guardrail_flags"] or "[]")
        if flags:
            guardrail_triggers += 1
    for bucket in per_model.values():
        bucket["avg_latency_ms"] /= bucket["requests"]
        bucket["cost_usd"] = round(bucket["cost_usd"], 6)
    return {
        "total_requests": total,
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_cost_usd": round(total_cost, 6),
        "avg_latency_ms": round(avg_latency, 2),
        "guardrail_triggered_requests": guardrail_triggers,
        "per_model": per_model,
    }


# ---------------------------------------------------------------- helpers
def count_tokens(text: str) -> int:
    """Whitespace token approximation (NOT a real tokenizer)."""
    return len(text.split())


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    cfg = MODELS.get(model, {})
    return round(
        prompt_tokens / 1000 * float(cfg.get("price_per_1k_prompt_tokens", 0.0))
        + completion_tokens
        / 1000
        * float(cfg.get("price_per_1k_completion_tokens", 0.0)),
        6,
    )


def resolve_prompt(
    name: str, version: int | str | None, variables: dict
) -> tuple[str, int]:
    """Render a registry template. Returns (rendered_text, version_used)."""
    matches = [p for p in PROMPTS if p["name"] == name]
    if not matches:
        raise HTTPException(status_code=404, detail=f"unknown prompt: {name!r}")
    if version in (None, "latest"):
        chosen = max(matches, key=lambda p: int(p["version"]))
    else:
        chosen = next(
            (p for p in matches if int(p["version"]) == int(version)), None
        )
        if chosen is None:
            raise HTTPException(
                status_code=404,
                detail=f"prompt {name!r} has no version {version!r}",
            )
    try:
        return chosen["template"].format(**variables), int(chosen["version"])
    except KeyError as exc:
        raise HTTPException(
            status_code=400, detail=f"missing template variable: {exc}"
        ) from exc


# ---------------------------------------------------------------- API
app = FastAPI(title="LLM Gateway + Observability")


class Message(BaseModel):
    role: str = "user"
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    model: str | None = None
    prompt_name: str | None = None
    prompt_version: int | str | None = None
    variables: dict = Field(default_factory=dict)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "default_model": DEFAULT_MODEL}


@app.get("/prompts")
def list_prompts() -> dict:
    names: dict[str, list[int]] = {}
    for p in PROMPTS:
        names.setdefault(p["name"], []).append(int(p["version"]))
    return {"prompts": {k: sorted(v) for k, v in names.items()}}


@app.post("/v1/chat")
def chat(req: ChatRequest) -> dict:
    model = req.model or DEFAULT_MODEL
    if model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model!r}")
    provider_name = MODELS[model]["provider"]

    # Build the prompt: registry template wins when prompt_name is given.
    prompt_version = None
    if req.prompt_name:
        prompt, prompt_version = resolve_prompt(
            req.prompt_name, req.prompt_version, req.variables
        )
    else:
        if not req.messages:
            raise HTTPException(
                status_code=400,
                detail="provide messages, or prompt_name + variables",
            )
        prompt = "\n".join(m.content for m in req.messages)

    # Guardrails on the prompt, before any provider call.
    flags: list[str] = []
    prompt, pii_found = redact_pii(prompt)
    flags += [f"pii_redacted:{k}" for k in pii_found]
    blocked = find_blocked(prompt, BLOCKED_WORDS)
    if blocked:
        raise HTTPException(
            status_code=400, detail=f"blocked content: {blocked}"
        )
    prompt, truncated = truncate(prompt, MAX_PROMPT_CHARS)
    if truncated:
        flags.append("prompt_truncated")

    started = time.perf_counter()
    try:
        completion = get_provider(provider_name).complete(prompt)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    latency_ms = (time.perf_counter() - started) * 1000

    # Guardrails on the completion, before it leaves the gateway.
    completion, pii_found = redact_pii(completion)
    flags += [f"completion_pii_redacted:{k}" for k in pii_found]
    completion, truncated = truncate(completion, MAX_COMPLETION_CHARS)
    if truncated:
        flags.append("completion_truncated")

    prompt_tokens = count_tokens(prompt)
    completion_tokens = count_tokens(completion)
    cost = estimate_cost(model, prompt_tokens, completion_tokens)

    log_request(
        {
            "ts": time.time(),
            "model": model,
            "provider": provider_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms": round(latency_ms, 2),
            "cost_usd": cost,
            "prompt_name": req.prompt_name,
            "prompt_version": prompt_version,
            "guardrail_flags": flags,
            "status": "ok",
        }
    )
    return {
        "model": model,
        "provider": provider_name,
        "completion": completion,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost_usd": cost,
        },
        "latency_ms": round(latency_ms, 2),
        "guardrail_flags": flags,
    }


@app.get("/metrics")
def metrics() -> dict:
    return aggregate_metrics()
