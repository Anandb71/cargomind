"""One bounded wire-JSON contract for HTTP and conversational tool adapters."""
import json

MAX_BODY = 262_144


class InvalidPayload(ValueError):
    """Public-safe parser error that never embeds rejected input."""


def strict_object(raw: str | bytes | bytearray) -> dict:
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > MAX_BODY:
        raise InvalidPayload("Scenario exceeds the bounded request size.")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate key")
            value[key] = item
        return value

    def finite(_):
        raise ValueError("Non-finite number")

    try:
        payload = json.loads(encoded, object_pairs_hook=unique_object, parse_constant=finite)
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidPayload("Invalid finite JSON object or duplicate field.") from None
    if not isinstance(payload, dict):
        raise InvalidPayload("Expected a JSON object.")
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 12:
            raise InvalidPayload("JSON exceeds the nesting limit.")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
    return payload
