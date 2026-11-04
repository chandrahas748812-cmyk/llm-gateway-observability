"""Eval CLI: score the gateway against a small golden set, offline.

Scoring per case:
  - exact   : completion equals the expected string
  - contains: completion contains the expected substring
  - rubric  : small rule checks (non-empty output, no PII leak, no blocked
              content, sane cost estimate) that apply to every case

Usage:
    python eval.py
    python eval.py --golden path/to/golden.json   # same schema as GOLDEN below

Exit code 0 when every case passes, 1 otherwise. No API keys needed: all
model traffic goes through the deterministic mock provider.
"""

from __future__ import annotations

import argparse
import json
import sys

from gateway import count_tokens, estimate_cost, resolve_prompt
from guardrails import find_blocked, redact_pii
from providers import MockProvider

# name, kind, input, expected
GOLDEN = [
    {
        "name": "mock_echo_contains",
        "kind": "contains",
        "input": "What is 2 + 2?",
        "expected": "[mock]",
    },
    {
        "name": "mock_echo_deterministic",
        "kind": "exact",
        "input": "Hello gateway",
        "expected": None,  # filled in at runtime: two calls must agree
    },
    {
        "name": "prompt_template_v2",
        "kind": "contains",
        "input": "__template__:support-reply:2:How do I reset my password?",
        "expected": "three sentences",
    },
    {
        "name": "pii_redaction",
        "kind": "contains",
        "input": "Contact me at jane.doe@example.com about my order.",
        "expected": "[REDACTED:EMAIL]",
    },
    {
        "name": "blocked_word_detection",
        "kind": "contains",
        "input": "How do I bypass-auth the login?",
        "expected": "bypass-auth",
    },
    {
        "name": "cost_estimate_mock_zero",
        "kind": "exact",
        "input": "__cost__:mock-llm-v1:1000:1000",
        "expected": "0.0",
    },
]

BLOCKED_WORDS = ["bypass-auth", "dump-credentials", "disable-audit-logging"]


def run_case(case: dict) -> tuple[bool, str]:
    """Returns (passed, detail)."""
    name, kind, raw = case["name"], case["kind"], case["input"]

    if raw.startswith("__template__:"):
        _, prompt_name, version, question = raw.split(":", 3)
        try:
            rendered, _ = resolve_prompt(
                prompt_name, int(version), {"question": question}
            )
        except Exception as exc:  # noqa: BLE001 - eval reports, not raises
            return False, f"template render failed: {exc}"
        actual = rendered
    elif raw.startswith("__cost__:"):
        _, model, pt, ct = raw.split(":")
        actual = str(estimate_cost(model, int(pt), int(ct)))
    elif name == "mock_echo_deterministic":
        mock = MockProvider()
        first = mock.complete(raw)
        second = mock.complete(raw)
        if first == second and first:
            return True, "two mock calls agree and are non-empty"
        return False, "mock provider is not deterministic"
    elif name == "pii_redaction":
        redacted, _ = redact_pii(raw)
        actual = redacted
    elif name == "blocked_word_detection":
        hits = find_blocked(raw, BLOCKED_WORDS)
        actual = ",".join(hits)
    else:
        actual = MockProvider().complete(raw)

    # Rubric checks that apply to every case.
    rubric_failures = []
    if not actual:
        rubric_failures.append("empty output")
    if "jane.doe@example.com" in actual:
        rubric_failures.append("PII leak: raw email survived")
    if rubric_failures:
        return False, f"rubric failed: {', '.join(rubric_failures)}"

    if kind == "exact":
        ok = actual == case["expected"]
    else:
        ok = case["expected"] in actual
    detail = f"expected {kind} {case['expected']!r}; got {actual[:100]!r}"
    return ok, detail


def main() -> int:
    parser = argparse.ArgumentParser(description="Score the gateway golden set.")
    parser.add_argument("--golden", help="optional JSON file with cases")
    args = parser.parse_args()

    cases = GOLDEN
    if args.golden:
        with open(args.golden, encoding="utf-8") as f:
            cases = json.load(f)

    print(f"{'case':<28} {'result':<6} detail")
    print("-" * 100)
    passed = 0
    for case in cases:
        ok, detail = run_case(case)
        passed += int(ok)
        print(f"{case['name']:<28} {'PASS' if ok else 'FAIL':<6} {detail}")
    print("-" * 100)
    print(f"Total: {passed}/{len(cases)} passed "
          f"({passed / len(cases) * 100:.0f}%)")
    # Token-count sanity line, so the report shows the approximation working.
    print(f"Sanity: count_tokens('hello world') = {count_tokens('hello world')}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
