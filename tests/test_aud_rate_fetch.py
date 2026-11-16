"""Opt-in Frankfurter AUD rate. No default rate, and a stale quote fails closed."""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.budgets import Limits
from codehelm.orchestrator.durable import DispatchBudget, DurableScheduler
from codehelm.orchestrator.fx import FRANKFURTER_SOURCE, _RefuseRedirect, fetch_aud_per_usd
from codehelm.orchestrator.monthly_budget import MonthlyPolicy
from codehelm.planning.planner import parse_plan
from urllib.error import HTTPError, URLError

runner = CliRunner()
NOW = datetime(2026, 11, 17, tzinfo=timezone.utc)


class _Body:
    status = 200

    def __init__(self, payload: bytes, url: str = "https://api.frankfurter.app/latest?from=USD&to=AUD"):
        self.payload = payload
        self.url = url

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _payload(rate="1.54", day="2026-11-16", base="USD", amount=1) -> bytes:
    return json.dumps({
        "amount": amount, "base": base, "date": day, "rates": {"AUD": rate},
    }).encode()


def _open(body: _Body):
    class _Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://api.frankfurter.app/")
            assert timeout == 20
            return body
    return _Opener()


def test_fetch_returns_the_source_rate_and_date():
    body = _Body(b'{"amount":1.0,"base":"USD","date":"2026-11-16","rates":{"AUD":1.54}}')
    with patch("codehelm.orchestrator.fx.build_opener", return_value=_open(body)):
        rate, source, as_of = fetch_aud_per_usd(NOW)
    assert rate == Decimal("1.54")
    assert source == FRANKFURTER_SOURCE
    assert as_of == "2026-11-16"
    five_days = _Body(_payload(day="2026-11-12"))
    with patch("codehelm.orchestrator.fx.build_opener", return_value=_open(five_days)):
        rate, source, as_of = fetch_aud_per_usd(NOW)
    assert rate == Decimal("1.54")
    assert source == FRANKFURTER_SOURCE
    assert as_of == "2026-11-12"


@pytest.mark.parametrize("payload,error", [
    (_payload(day="2026-11-01"), "stale"),
    (_payload(day="2026-11-18"), "stale"),
    (_payload(rate="0"), "did not return"),
    (_payload(base="EUR"), "did not return"),
    (b"not-json", "did not return"),
    (b'{"base": "USD"}', "did not return"),
])
def test_fetch_fails_closed(payload, error):
    body = _Body(payload)
    with patch("codehelm.orchestrator.fx.build_opener", return_value=_open(body)):
        with pytest.raises(ValueError, match=error):
            fetch_aud_per_usd(NOW)


def test_redirect_http_and_network_errors_fail_closed():
    with pytest.raises(ValueError, match="redirect"):
        _RefuseRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.com")
    with patch("codehelm.orchestrator.fx.build_opener") as build:
        build.return_value.open.side_effect = HTTPError(
            "https://api.frankfurter.app/latest", 500, "err", hdrs=None, fp=BytesIO(b""))
        with pytest.raises(ValueError, match="HTTP 500"):
            fetch_aud_per_usd(NOW)
    with patch("codehelm.orchestrator.fx.build_opener") as build:
        build.return_value.open.side_effect = URLError("down")
        with pytest.raises(ValueError, match="fetch failed"):
            fetch_aud_per_usd(NOW)
    moved = _Body(_payload(), url="https://example.com/rate")
    with patch("codehelm.orchestrator.fx.build_opener", return_value=_open(moved)):
        with pytest.raises(ValueError, match="redirect"):
            fetch_aud_per_usd(NOW)


def test_claim_stores_the_fetched_timestamp(tmp_path):
    plan = parse_plan(json.dumps({
        "epic_title": "Epic", "epic_description": "Description",
        "stories": [{
            "id": "S1", "title": "Story", "description": "Implement",
            "acceptance_criteria": ["Pass"], "dependencies": [],
        }],
    }))
    database = tmp_path / "state.db"
    scheduler = DurableScheduler(database, plan, "E1")
    aud = Decimal("1.50") * Decimal("1.54")
    scheduler.claim_budgeted(
        DispatchBudget(), "claude", 100, Decimal("0.50"), Limits(1000, Decimal("10")),
        MonthlyPolicy(), aud, aud_rate=Decimal("1.54"), aud_rate_source=FRANKFURTER_SOURCE,
        aud_rate_as_of="2026-11-16")
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT estimated_aud, rate_aud_per_usd, rate_source, rate_as_of FROM aud_reservations"
        ).fetchone()
    assert row == (str(aud), "1.54", FRANKFURTER_SOURCE, "2026-11-16")


def test_schedule_dry_run_does_not_fetch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "epic_title": "Epic", "epic_description": "Description",
        "stories": [{
            "id": "S1", "title": "Story", "description": "Implement",
            "acceptance_criteria": ["Pass"], "dependencies": [],
        }],
    }), encoding="utf-8")
    with patch("codehelm.cli.main.fetch_aud_per_usd") as fetch:
        result = runner.invoke(app, [
            "schedule-durable", "--plan-file", str(plan), "--prefix", "E1",
            "--reserve-tokens", "100", "--reserve-usd", "0.50",
            "--limit-tokens", "1000", "--limit-usd", "10",
            "--additional-usd", "1.50", "--fetch-aud-rate",
        ])
    assert result.exit_code == 0, result.output
    assert "Would fetch" in result.output
    assert "Not recorded" in result.output
    fetch.assert_not_called()
    combined = runner.invoke(app, [
        "schedule-durable", "--plan-file", str(plan), "--prefix", "E1",
        "--reserve-tokens", "100", "--reserve-usd", "0.50",
        "--limit-tokens", "1000", "--limit-usd", "10",
        "--additional-usd", "1.50", "--aud-per-usd", "1.54",
        "--aud-rate-source", "desk", "--fetch-aud-rate",
    ])
    assert combined.exit_code == 1
    assert "cannot be combined" in combined.output


def test_quote_fetch_prints_the_date_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch("codehelm.cli.main.fetch_aud_per_usd",
               return_value=(Decimal("1.54"), FRANKFURTER_SOURCE, "2026-11-16")):
        result = runner.invoke(app, ["aud-quote", "1.50", "--fetch-aud-rate"])
    assert result.exit_code == 0, result.output
    assert "2.3100 AUD" in result.output
    assert FRANKFURTER_SOURCE in result.output
    assert "2026-11-16" in result.output
    assert "Not recorded" in result.output
    assert not (tmp_path / ".codehelm" / "state.db").exists()
