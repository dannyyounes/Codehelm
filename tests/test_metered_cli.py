import json
from codehelm.providers.usage import parse_cli_usage


def test_claude_result_envelope():
    payload = {"type": "result", "usage": {"input_tokens": 80, "output_tokens": 20},
               "total_cost_usd": 0.02}
    usage = parse_cli_usage("claude", json.dumps(payload))
    assert usage.tokens == 100
    assert str(usage.usd) == "0.02"


def test_codex_jsonl_turn_completed():
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "abc"}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 120, "output_tokens": 40}})
    ]
    usage = parse_cli_usage("codex", "\n".join(lines))
    assert usage.tokens == 160
    assert usage.usd is None


def test_gemini_stats_envelope():
    payload = {"stats": {"models": {"gemini-model": {"tokens": {
        "input": 200, "candidates": 50}}}}}
    usage = parse_cli_usage("gemini", json.dumps(payload))
    assert usage.tokens == 250
    assert usage.usd is None


def test_invalid_output_keeps_usage_unknown():
    assert parse_cli_usage("codex", "Agent completed successfully") is None


def test_cursor_result_envelope():
    payload = {"type": "result", "result": "OK", "usage": {
        "inputTokens": 6779, "outputTokens": 26, "cacheReadTokens": 3968}}
    usage = parse_cli_usage("cursor", json.dumps(payload))
    assert usage.input_tokens == 6779 + 3968 and usage.output_tokens == 26
    assert usage.usd is None and usage.source == "cursor:structured-json"
    assert parse_cli_usage("cursor", json.dumps({"type": "result", "usage": {}})) is None
    bad_cache = {"type": "result", "usage": {
        "inputTokens": 1, "outputTokens": 1, "cacheReadTokens": "3"}}
    assert parse_cli_usage("cursor", json.dumps(bad_cache)) is None
