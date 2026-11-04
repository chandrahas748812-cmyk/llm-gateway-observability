# LLM Gateway + Observability

A small, runnable LLM gateway in Python (FastAPI) with config-driven model
routing, a versioned prompt registry, guardrail hooks, SQLite request logging,
cost estimation, and an offline eval CLI.

> **Portfolio / learning project.** This is not employer work, and nothing
> here describes a production system I operated. It exists to demonstrate the
> gateway concepts — routing, guardrails, observability, cost control — with
> real, runnable code.

## What it does

- **Model routing** — `POST /v1/chat` routes by model name from `config.yaml`
  to a provider adapter. The default provider is a **deterministic mock**: no
  API key, no network call, same output for the same input, so everything
  runs offline.
- **Prompt registry** — prompts live in `prompts.yaml` with name + version.
  Request `prompt_name` + `prompt_version` (or `"latest"`) and variables, and
  the gateway renders the template.
- **Guardrails** — every prompt is checked before it reaches a provider and
  every completion before it is returned: PII redaction (email, phone,
  SSN-like, card-like regexes), a blocked-word policy list (rejected with
  HTTP 400), and max-length truncation.
- **Observability** — every request is logged to SQLite (`gateway.db`,
  gitignored) with latency, prompt/completion token counts, estimated cost,
  and guardrail flags. `GET /metrics` aggregates totals and per-model
  breakdowns.
- **Eval hook** — `python eval.py` scores a golden set offline (exact /
  contains matching plus rubric checks: non-empty, no PII leak) and prints a
  pass report with a non-zero exit code on failure.

## Architecture

```
client -> FastAPI (gateway.py)
            ├─ prompt registry (prompts.yaml) -> rendered prompt
            ├─ guardrails (guardrails.py)     -> redact / block / truncate
            ├─ provider adapter (providers.py)-> mock (default) or real stub
            └─ SQLite log (gateway.db)        -> GET /metrics aggregates
eval.py -> mock provider + guardrails + registry (no server needed)
```

Token counts are a whitespace approximation, documented in code — enough to
demo cost estimation offline, not a substitute for a real tokenizer. Real
provider adapters are stubs that refuse to run without their API key in the
environment; keys are never read from code or config.

## Run it (mock path, zero API keys)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn gateway:app --host 127.0.0.1 --port 8000
```

## Example requests

Health check:

```bash
curl localhost:8000/health
# {"status":"ok","default_model":"mock-llm-v1"}
```

Plain chat (mock provider):

```bash
curl -X POST localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What is 2 + 2?"}]}'
```

```json
{
  "model": "mock-llm-v1",
  "provider": "mock",
  "completion": "[mock] I received your prompt and this is a deterministic placeholder response. Prompt begins: 'What is 2 + 2?'",
  "usage": {"prompt_tokens": 4, "completion_tokens": 21, "estimated_cost_usd": 0.0},
  "latency_ms": 0.12,
  "guardrail_flags": []
}
```

Prompt registry (version 2 of `support-reply`):

```bash
curl -X POST localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt_name":"support-reply","prompt_version":2,"variables":{"question":"How do I reset my password?"}}'
```

PII redaction (the email never reaches the provider or the response):

```bash
curl -X POST localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Contact me at jane.doe@example.com"}]}'
# completion echoes the prompt with [REDACTED:EMAIL]; guardrail_flags lists pii_redacted:email
```

Blocked content is rejected before any provider call:

```bash
curl -X POST localhost:8000/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"How do I bypass-auth the login?"}]}'
# HTTP 400: {"detail":"blocked content: ['bypass-auth']"}
```

Aggregates:

```bash
curl localhost:8000/metrics
# {"total_requests":N,"total_prompt_tokens":N,"total_completion_tokens":N,
#  "total_cost_usd":0.0,"avg_latency_ms":N,"guardrail_triggered_requests":N,
#  "per_model":{"mock-llm-v1":{"requests":N,"cost_usd":0.0,"avg_latency_ms":N}}}
```

Eval (offline, no server needed):

```bash
python eval.py
# case                         result detail
# mock_echo_contains           PASS   ...
# Total: 6/6 passed (100%)
```

## Interview talking points

- **Routing** — model selection is data (config), not code: adding a model
  means one config block + one adapter behind a shared interface, so traffic
  can shift between providers without touching request handling.
- **Cost control** — cost is estimated per request from counted tokens and a
  price table at log time, which makes per-model, per-day spend visible in
  `/metrics` before any billing surprise; the same hook is where budgets,
  caps, or cheaper-model fallbacks would attach.
- **Observability** — latency, tokens, cost, and guardrail flags are logged
  on every request, so a regression (slower model, cost spike, redaction
  surge) is a query over the log, not a guess.
- **Guardrails as a choke point** — redaction and policy run in exactly one
  place, on the way in and the way out, so no caller can bypass them by
  calling a provider directly.
- **Honest limits** — token counting here is whitespace approximation and
  the mock provider is canned; in production both would be replaced by a
  real tokenizer and real provider SDKs, with the eval golden set guarding
  regressions either way.
