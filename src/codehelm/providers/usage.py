"""Fail-closed parsing of structured CLI usage; never infer cost from text."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    usd: Decimal | None
    source: str

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def parse_usage(provider: str, output: str) -> Usage | None:
    """Recognize explicit JSON usage fields; ambiguous/missing values stay unknown."""
    try:
        payload = json.loads(output)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if provider == "claude":
        usage = payload.get("usage")
        cost = payload.get("total_cost_usd")
        if not isinstance(usage, dict):
            return None
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
    elif provider == "codex":
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
        cost = payload.get("total_cost_usd")
    elif provider == "gemini":
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        inp, out = usage.get("input_tokens"), usage.get("output_tokens")
        cost = payload.get("total_cost_usd")
    else:
        return None
    if type(inp) is not int or type(out) is not int or inp < 0 or out < 0:
        return None
    if cost is None:
        dollars = None
    else:
        try:
            dollars = Decimal(str(cost))
        except (InvalidOperation, ValueError):
            return None
        if not dollars.is_finite() or dollars < 0:
            return None
    return Usage(inp, out, dollars, f"{provider}:structured-json")


def parse_cli_usage(provider: str, output: str) -> Usage | None:
    """Parse recognized provider envelopes, including Codex JSONL token events.

    Do not infer USD cost from subscriptions or token counts.
    """
    direct = parse_usage(provider, output)
    if direct is not None:
        return direct
    if provider == "claude":
        try:
            data = json.loads(output)
            if isinstance(data, dict) and data.get("type") == "result":
                usage = data.get("usage", {})
                if isinstance(usage, dict):
                    return parse_usage("claude", json.dumps({
                        "usage": usage, "total_cost_usd": data.get("total_cost_usd")}))
        except (ValueError, TypeError):
            pass
    if provider == "gemini":
        try:
            data = json.loads(output)
            stats = data.get("stats", {}).get("models", {})
            if isinstance(stats, dict) and stats:
                totals = [v.get("tokens", {}) for v in stats.values() if isinstance(v, dict)]
                inp = sum(x.get("input", 0) for x in totals)
                out = sum(x.get("candidates", 0) for x in totals)
                if totals and all(isinstance(x.get("input"), int) and
                                  isinstance(x.get("candidates"), int) for x in totals):
                    return parse_usage("gemini", json.dumps({
                        "usage": {"input_tokens": inp, "output_tokens": out}}))
        except (ValueError, TypeError, AttributeError):
            pass
    if provider == "codex":
        try:
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            usage_events = [e["usage"] for e in events if isinstance(e, dict)
                            and e.get("type") == "turn.completed"
                            and isinstance(e.get("usage"), dict)]
            if usage_events:
                # Sum completed turns; a final cumulative event may require
                # different handling, so report only the last event conservatively.
                last = usage_events[-1]
                return parse_usage("codex", json.dumps({"usage": last}))
        except (ValueError, TypeError, KeyError):
            pass
    return None
