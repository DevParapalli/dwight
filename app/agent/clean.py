import re
import unicodedata
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import phonenumbers
from rapidfuzz.distance import Levenshtein

EXCEL_EPOCH = date(1899, 12, 30)
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_NUMERIC_STRING_RE = re.compile(r"^\d{4,10}$")
_CURRENCY_STRIP_RE = re.compile(r"[₹$,\s]|INR|USD|EUR")


def clean_string(value) -> str | None:
    if value in (None, ""):
        return None
    s = unicodedata.normalize("NFKC", str(value)).strip()
    s = re.sub(r"\s+", " ", s)
    return s or None


def clean_email(value) -> str | None:
    s = clean_string(value)
    return s.lower() if s else None


def clean_phone(value, default_region: str = "IN") -> str | None:
    """Returns E.164, or None if unparseable (caller/validation decides what to do
    with that -- this function doesn't guess)."""
    s = clean_string(value)
    if s is None:
        return None
    try:
        parsed = phonenumbers.parse(s, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _valid_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def _slash_date_ambiguous(raw: str) -> bool:
    m = _SLASH_DATE_RE.match(raw)
    if not m:
        return False
    a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a == b:
        return False
    day_first_valid = _valid_date(year, b, a)
    month_first_valid = _valid_date(year, a, b)
    return day_first_valid and month_first_valid


def parse_date_value(value, accepted_formats: list[str]) -> tuple[date | None, bool]:
    """Returns (parsed_date, ambiguous). ambiguous=True means the value could
    plausibly be two different valid dates and nothing here should guess which."""
    if value in (None, ""):
        return None, False

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A native numeric cell (from xlsx) is always meant as an excel serial --
        # unlike a numeric-looking CSV string, there's no format ambiguity here.
        return EXCEL_EPOCH + timedelta(days=int(value)), False

    raw = str(value).strip()
    if not raw:
        return None, False

    if _slash_date_ambiguous(raw):
        return None, True

    if "excel_serial" in accepted_formats and _NUMERIC_STRING_RE.match(raw) and len(raw) <= 6:
        try:
            d = EXCEL_EPOCH + timedelta(days=int(raw))
            if date(1990, 1, 1) <= d <= date(2100, 1, 1):
                return d, False
        except (ValueError, OverflowError):
            pass

    if "epoch_seconds" in accepted_formats and _NUMERIC_STRING_RE.match(raw) and len(raw) in (9, 10):
        try:
            d = datetime.fromtimestamp(int(raw), tz=UTC).date()
            if date(1990, 1, 1) <= d <= date(2100, 1, 1):
                return d, False
        except (ValueError, OSError, OverflowError):
            pass

    for fmt in accepted_formats:
        if fmt in ("excel_serial", "epoch_seconds"):
            continue
        try:
            # The result is a date, not a moment. A birth date or a hire date
            # has no timezone, and attaching one would invent an instant the
            # source never recorded -- hence the suppression below.
            return datetime.strptime(raw, fmt).date(), False  # noqa: DTZ007
        except ValueError:
            continue

    return None, False


def clean_number(value) -> tuple[Decimal | None, str | None]:
    """Returns (amount, currency). Currency is inferred from the string itself;
    default is INR when no symbol/code is present, per the schema's default."""
    if value in (None, ""):
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value)), "INR"

    s = str(value).strip()
    currency = "INR"
    if "USD" in s.upper():
        currency = "USD"
    elif "EUR" in s.upper():
        currency = "EUR"

    digits = _CURRENCY_STRIP_RE.sub("", s).strip()
    if not digits:
        return None, currency
    try:
        return Decimal(digits), currency
    except InvalidOperation:
        return None, currency


def coerce_enum(value, allowed_values: list[str],
                max_auto_edit_distance: int = 1) -> tuple[str | None, int, str | None]:
    """Returns (coerced_value_or_None, edit_distance, nearest_value).

    `nearest` is returned even when it was too far to apply automatically: that
    is precisely the case where a human is asked, and the question is far easier
    to answer when it names the closest allowed value rather than only how far
    away it was."""
    s = clean_string(value)
    if s is None:
        return None, 0, None

    normalized = s.lower()
    for allowed in allowed_values:
        if normalized == allowed.lower():
            return allowed, 0, allowed

    best_allowed, best_distance = None, None
    for allowed in allowed_values:
        d = Levenshtein.distance(normalized, allowed.lower())
        if best_distance is None or d < best_distance:
            best_allowed, best_distance = allowed, d

    if best_distance is not None and best_distance <= max_auto_edit_distance:
        return best_allowed, best_distance, best_allowed
    return None, best_distance if best_distance is not None else 999, best_allowed
