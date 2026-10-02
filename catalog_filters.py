"""One strict filter contract for model output, RPC and direct SQL callers."""
from collections.abc import Mapping
import json

SKU = "\u0410\u0440\u0442\u0438\u043a\u0443\u043b"
CATEGORY = "\u041a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044f"
DESCRIPTION = "\u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435"
PRICE = "\u0426\u0435\u043d\u0430"
ANY_VALUE = "\u043d\u0435 \u0438\u043c\u0435\u0435\u0442 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f"
MAX_PRICE = 2 ** 31 - 1
CATALOG_LIMIT = 100
ALIASES = {
    "sku": SKU, "category": CATEGORY, "description": DESCRIPTION, "price": PRICE,
    **{key.casefold(): key for key in (SKU, CATEGORY, DESCRIPTION, PRICE)},
}


class FilterValidationError(ValueError):
    """A fixed diagnostic, without raw model text or user data."""


def validate_text(value, maximum):
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > maximum:
        raise FilterValidationError("Expected bounded nonempty text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise FilterValidationError("Invalid text encoding") from None
    return value


def _price(value):
    if not isinstance(value, Mapping) or not value or set(value) - {"<", ">", "<=", ">=", "="}:
        raise FilterValidationError("Invalid price operators")
    lower, upper = 0, MAX_PRICE
    result = {}
    for operator, amount in value.items():
        if type(amount) is not int or not 0 <= amount <= MAX_PRICE:
            raise FilterValidationError("Price must be a nonnegative 32-bit integer")
        result[operator] = amount
        if operator == "<":
            upper = min(upper, amount - 1)
        elif operator == "<=":
            upper = min(upper, amount)
        elif operator == ">":
            lower = max(lower, amount + 1)
        elif operator == ">=":
            lower = max(lower, amount)
        else:
            lower, upper = max(lower, amount), min(upper, amount)
    if lower > upper:
        raise FilterValidationError("Contradictory price range")
    return result


def normalize_filters(filters, *, keep_removals=False):
    if not isinstance(filters, Mapping):
        raise FilterValidationError("Catalog filters must be an object")
    result, seen = {}, set()
    for key, value in filters.items():
        if not isinstance(key, str) or key.strip().casefold() not in ALIASES:
            raise FilterValidationError("Unsupported catalog field")
        canonical = ALIASES[key.strip().casefold()]
        if canonical in seen:
            raise FilterValidationError("Duplicate catalog field alias")
        seen.add(canonical)
        if value is None or (isinstance(value, str) and value.strip().casefold() == ANY_VALUE):
            if keep_removals:
                result[canonical] = None
            continue
        result[canonical] = _price(value) if canonical == PRICE else validate_text(value, 100).strip()
    return result


def merge_filter_patch(previous, patch):
    result = normalize_filters(previous)
    for key, value in normalize_filters(patch, keep_removals=True).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FilterValidationError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise FilterValidationError("Nonfinite JSON number")


def load_json_object(value, *, limit=16384):
    try:
        if isinstance(value, bytes):
            if len(value) > limit:
                raise ValueError()
            value = value.decode("utf-8")
        elif not isinstance(value, str) or len(value.encode("utf-8")) > limit:
            raise ValueError()
        result = json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise FilterValidationError("Invalid or oversized JSON object") from None


def parse_model_patch(value):
    validate_text(value, 4096)
    if len(value.encode("utf-8")) > 4096:
        raise FilterValidationError("Model filter response is too large")
    value = value.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) < 3 or lines[0] != "```json" or lines[-1] != "```":
            raise FilterValidationError("Expected one complete JSON object")
        value = "\n".join(lines[1:-1])
    return normalize_filters(load_json_object(value, limit=4096), keep_removals=True)
