"""Convert an explicit USD amount into AUD with an operator-supplied rate.

There is no default rate and no network fetch. A missing rate, source, or
amount fails closed. The result is not a provider bill.
"""
from decimal import Decimal, InvalidOperation


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
