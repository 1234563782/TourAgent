"""Conservative model-only projections; never mutate retrieved candidates."""

from .contracts import Candidate


def _without_empty_values(value):
    if isinstance(value, dict):
        return {key: _without_empty_values(item) for key, item in value.items()
                if item is not None and item != ""
                and not (key == "truncated_fields" and item == [])}
    if isinstance(value, list):
        return [_without_empty_values(item) for item in value]
    return value


def _remove_duplicate(values, key, expected):
    if key in values and expected is not None and values[key] == expected:
        values.pop(key)


def candidate_selection_input(candidate: Candidate, mode: str) -> dict:
    """Keep business facts and uncertainty; strip only known selection redundancy."""
    original = candidate.model_dump(mode="json")
    if mode == "full":
        return original
    if mode != "compact":
        raise ValueError("Candidate input mode must be full or compact")

    projected = _without_empty_values(original)
    # Price completeness is a selection fact, including an explicitly missing amount.
    projected["quote"] = original["quote"]
    projected.pop("source", None)
    projected.pop("observed_at", None)
    details = projected.get("details", {})
    if candidate.kind == "train":
        _remove_duplicate(details, "seat_type", candidate.seat_class)
    elif candidate.kind == "flight":
        _remove_duplicate(details, "cabinClass", candidate.seat_class)
        _remove_duplicate(projected, "transport_mode", "flight")
    else:
        _remove_duplicate(details, "inbound", False)
        details.pop("hotelId", None)
        detail = details.get("hotel_detail")
        if isinstance(detail, dict):
            detail.pop("observed_at", None)
            requested = detail.get("requested")
            if isinstance(requested, dict):
                requested.pop("hotelId", None)
            for key in ("date_verified", "inventory_verified"):
                _remove_duplicate(detail, key, projected.get(key))
    return projected
