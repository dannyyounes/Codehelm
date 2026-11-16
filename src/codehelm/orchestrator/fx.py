"""Convert a USD amount into AUD.

The explicit path uses only an operator-supplied rate. An opt-in fetch reads
AUD per USD from frankfurter.app and keeps that source date. There is no
default rate. A missing, failed, or stale fetch fails closed. The result is
not a provider bill.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


FRANKFURTER_SOURCE = "frankfurter.app"
FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=USD&to=AUD"
_MAX_RATE_AGE_DAYS = 5
_MAX_BODY = 10000


class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("AUD rate source refused a redirect")


def resolve_additional_aud(aud_text: str, usd_text: str | None,
                           rate_text: str | None, source: str | None) -> tuple[Decimal, Decimal | None, str | None]:
    """Return AUD amount, rate, and source.

    The direct AUD amount is used when no conversion field is set. Conversion
    requires a positive USD amount, a positive AUD-per-USD rate, and a short
    source label. It cannot be combined with a nonzero direct AUD amount.
    """
    try:
        aud = Decimal(str(aud_text).strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid AUD reservation") from exc
    source_text = (source or "").strip()
    converting = usd_text is not None or rate_text is not None or bool(source_text)
    if not converting:
        if not aud.is_finite() or aud < 0:
            raise ValueError("Invalid AUD reservation")
        return aud, None, None
    if aud != 0:
        raise ValueError("Pass either an AUD amount or a USD amount with a rate, not both")
    if usd_text is None or rate_text is None or not source_text:
        raise ValueError("AUD conversion requires a USD amount, an AUD-per-USD rate, and a rate source")
    if "\x00" in source_text or len(source_text) > 200:
        raise ValueError("AUD rate source must be a short label")
    try:
        usd = Decimal(str(usd_text).strip())
        rate = Decimal(str(rate_text).strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError("AUD conversion rate and USD amount must be positive and finite") from exc
    if not usd.is_finite() or usd <= 0 or not rate.is_finite() or rate <= 0:
        raise ValueError("AUD conversion rate and USD amount must be positive and finite")
    converted = usd * rate
    if not converted.is_finite() or converted < 0:
        raise ValueError("AUD conversion is not finite")
    return converted, rate, source_text


def fetch_aud_per_usd(now: datetime | None = None) -> tuple[Decimal, str, str]:
    """Return the Frankfurter AUD-per-USD rate, its source name, and its date.

    The date is the rate date from the response, not a clock time invented here.
    A redirect, a failed request, a missing rate, or a date more than five days
    old fails closed. This function does not supply a fallback rate.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("AUD rate timestamp must be timezone-aware")
    request = Request(FRANKFURTER_URL, headers={"Accept": "application/json"}, method="GET")
    try:
        with build_opener(_RefuseRedirect).open(request, timeout=20) as response:
            final = response.geturl() if hasattr(response, "geturl") else FRANKFURTER_URL
            if urlparse(final).netloc != "api.frankfurter.app":
                raise ValueError("AUD rate source refused a redirect")
            status = getattr(response, "status", None) or response.getcode()
            if status != 200:
                raise ValueError(f"AUD rate source returned HTTP {status}")
            body = response.read(_MAX_BODY + 1)
    except HTTPError as exc:
        raise ValueError(f"AUD rate source returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError("AUD rate fetch failed") from exc
    if len(body) > _MAX_BODY:
        raise ValueError("AUD rate source returned too much data")
    try:
        payload = json.loads(body.decode("utf-8"), parse_float=Decimal)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("AUD rate source did not return a USD/AUD rate") from exc
    if not isinstance(payload, dict) or payload.get("base") != "USD":
        raise ValueError("AUD rate source did not return a USD/AUD rate")
    amount = _decimal(payload.get("amount", 1), "USD amount")
    if amount != 1:
        raise ValueError("AUD rate source did not return a USD/AUD rate")
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        raise ValueError("AUD rate source did not return a USD/AUD rate")
    rate = _decimal(rates.get("AUD"), "AUD rate")
    if rate <= 0:
        raise ValueError("AUD rate source did not return a USD/AUD rate")
    raw_date = payload.get("date")
    if not isinstance(raw_date, str):
        raise ValueError("AUD rate source did not return a rate date")
    try:
        rate_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError("AUD rate source did not return a rate date") from exc
    age = (moment.date() - rate_date).days
    if age < 0 or age > _MAX_RATE_AGE_DAYS:
        raise ValueError(f"AUD rate from {FRANKFURTER_SOURCE} is stale ({raw_date})")
    return rate, FRANKFURTER_SOURCE, rate_date.isoformat()


def _decimal(value, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
        raise ValueError(f"AUD rate source did not return a {label}")
    try:
        number = Decimal(str(value).strip()) if isinstance(value, str) else Decimal(value)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"AUD rate source did not return a {label}") from exc
    if not number.is_finite():
        raise ValueError(f"AUD rate source did not return a {label}")
    return number
