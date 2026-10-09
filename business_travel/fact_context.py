"""Server-owned provenance and revision-scoped acknowledgment, not fact certification."""

from copy import deepcopy


def fact_context(subject):
    provenance = subject.get("provenance", {})
    confirmation = subject.get("fact_confirmation", {})
    confirmed = (
        confirmation.get("subject_id") == subject.get("id")
        and confirmation.get("revision") == subject.get("revision")
        and confirmation.get("confirmed") is True
    )
    fields = {}
    for name, value in subject.get("values", {}).items():
        origin = provenance.get(name, {})
        fields[name] = {
            "value": deepcopy(value),
            "source": origin.get("source", "unknown"),
            "evidence": deepcopy(subject.get("evidence", {}).get(name)),
            "review_status": origin.get("review_status", "unknown"),
            "user_confirmed": confirmed,
            # No enterprise fact-verification adapter is connected yet.
            "business_verified": False,
        }
    return {"subject_id": subject.get("id"), "revision": subject.get("revision"),
            "fields": fields, "scope": "user_acknowledgment_not_business_verification"}


def confirm_subject(subject, subject_id, revision):
    if subject.get("id") != subject_id or subject.get("revision") != revision:
        raise ValueError("stale_or_unknown_subject")
    updated = deepcopy(subject)
    updated["fact_confirmation"] = {
        "subject_id": subject_id, "revision": revision, "confirmed": True,
        "scope": "user_acknowledgment_not_business_verification",
    }
    updated["result"] = None
    return updated
