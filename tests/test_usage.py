import json
from decimal import Decimal
from codehelm.providers.usage import parse_usage


def test_claude_usage():
    result = parse_usage("claude", json.dumps({
        "usage": {"input_tokens": 120, "output_tokens": 30},
        "total_cost_usd": "0.004"}))
    assert result.tokens == 150
    assert result.usd == Decimal("0.004")


def test_codex_unknown_cost():
    result = parse_usage("codex", json.dumps({
        "usage": {"input_tokens": 10, "output_tokens": 5}}))
    assert result.tokens == 15
    assert result.usd is None


def test_reject_missing_or_invalid():
    assert parse_usage("gemini", "not-json") is None
    assert parse_usage("claude", '{"usage":{"input_tokens":1}}') is None
    assert parse_usage("claude", '{"usage":{"input_tokens":true,"output_tokens":1}}') is None
    assert parse_usage("claude", '{"usage":{"input_tokens":1,"output_tokens":1},"total_cost_usd":"NaN"}') is None
