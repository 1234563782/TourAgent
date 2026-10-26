"""Amount projections of deterministic checks, bound to published source chunks."""

from datetime import timedelta
from decimal import Decimal


ZERO = Decimal(0)
ALLOWANCE_KINDS = frozenset({"meal", "local_transport"})


def estimate_expense(facts, checker, *, max_nights, currency, effective_from=None, effective_to=None):
    """Reuse the published rule engine; never infer an over-limit payment policy."""
    if facts.kind not in {"hotel", "transport", *ALLOWANCE_KINDS}:
        return checker(facts)
    if facts.kind in ALLOWANCE_KINDS:
        return _estimate_allowance(
            facts, checker, max_days=max_nights, currency=currency,
            effective_from=effective_from, effective_to=effective_to)
    summary = {
        "scenario": facts.expense_scenario, "kind": facts.kind,
        "currency": facts.currency, "policy_currency": currency,
        "status": "pending", "reference_amount": None, "known_reference_amount": None,
        "submitted_amount": None, "standard_portion": None, "excess": None,
        "cap_total": None, "items": [], "approved": False,
    }

    def pending(reason, *fields):
        return {"status": "insufficient_information", "reason": reason,
                "missing_fields": list(fields), "approved": False,
                "source_clauses": [], "estimate": summary}

    if facts.kind == "transport" and (
            facts.check_out is not None or facts.nightly_amounts or facts.amount_scope != "single"):
        return pending("expense_amount_scope_conflict", "amount_scope")
    if facts.kind == "hotel":
        if facts.fare_amount is not None or facts.transport_amount_scope != "unknown":
            return pending("expense_amount_scope_conflict", "transport_amount_scope")
        if facts.check_out is not None:
            nights = (facts.check_out - facts.expense_date).days
            if not 1 <= nights <= max_nights:
                return pending("invalid_stay_period", "expense_date", "check_out")
        else:
            nights = 1
            if facts.amount_scope != "single" or (facts.nightly_amounts and len(facts.nightly_amounts) > 1):
                return pending("stay_period_required", "check_out")
        dates = [facts.expense_date + timedelta(days=index) for index in range(nights)]
    else:
        dates = [facts.expense_date]
    if any((effective_from and day < effective_from) or (effective_to and day > effective_to)
           for day in dates):
        return pending("expense_outside_published_period", "expense_date")

    amounts = {}
    if facts.kind == "hotel":
        if facts.nightly_amounts:
            amounts = {item.date: item.amount for item in facts.nightly_amounts}
            if (len(amounts) != len(facts.nightly_amounts) or not set(amounts) <= set(dates)
                    or (facts.amount is not None and (
                        facts.amount_scope != "stay_total" or set(amounts) != set(dates)
                        or sum(amounts.values(), ZERO) != facts.amount))):
                return pending("nightly_amounts_conflict", "nightly_amounts", "amount_scope")
        elif facts.amount_scope == "uniform_nightly" or (
                facts.amount_scope == "single" and len(dates) == 1):
            amounts = {day: facts.amount for day in dates}
        elif facts.amount_scope == "stay_total" and len(dates) == 1:
            amounts = {dates[0]: facts.amount}
        # A total cannot establish any particular nightly cost, even if below the sum of limits.
        if facts.amount_scope == "stay_total":
            summary["submitted_amount"] = _money(facts.amount)
        elif set(amounts) == set(dates) and all(value is not None for value in amounts.values()):
            summary["submitted_amount"] = _money(sum(amounts.values(), ZERO))
    else:
        if facts.fare_amount is not None and (facts.amount is None or facts.fare_amount > facts.amount):
            return pending("transport_breakdown_conflict", "amount", "fare_amount")
        if (facts.transport_amount_scope == "fare_only" and facts.fare_amount is not None
                and facts.fare_amount != facts.amount):
            return pending("transport_breakdown_conflict", "amount", "fare_amount")
        summary["submitted_amount"] = _money(facts.amount)
        amounts[dates[0]] = (facts.amount if facts.transport_amount_scope == "fare_only"
                             else facts.fare_amount)

    results = []
    for day in dates:
        unit_amount = amounts.get(day)
        # The checker sees one unit; the public result keeps the original scenario and scope.
        unit = facts.model_copy(update={"expense_date": day, "amount": unit_amount,
                                       "expense_scenario": "actual", "amount_scope": "single",
                                       "check_out": None, "nightly_amounts": None,
                                       "fare_amount": None})
        checked = checker(unit)
        results.append(checked)
        missing = list(checked.get("missing_fields", []))
        if facts.kind == "hotel" and unit_amount is None and len(dates) > 1:
            missing = [name for name in missing if name != "amount"] + ["nightly_amounts"]
        if facts.kind == "transport":
            for name, value in (("amount", facts.amount), ("currency", facts.currency)):
                if value is None:
                    missing.append(name)
            for name in ("date_verified", "taxes_confirmed"):
                if getattr(facts, name) is not True:
                    missing.append(name)
            if facts.currency is not None and facts.currency != currency:
                missing.append("currency")
            if unit_amount is None:
                missing.append("transport_amount_scope")
            if facts.basis != "ticket":
                missing.append("basis")
        # A class check alone says nothing about a mixed payment or unconfirmed amounts.
        eligible = checked.get("status") == "within_checked_rules" and not missing and unit_amount is not None
        cap = Decimal(checked["cap"]) if checked.get("cap") is not None else None
        comparable = (cap is not None and unit_amount is not None
                      and checked.get("status") in {"within_checked_rules", "over_standard"})
        item = {
            "date": day.isoformat(), "amount": _money(unit_amount), "cap": _money(cap),
            "reference_amount": _money(unit_amount) if eligible else None,
            "standard_portion": _money(min(unit_amount, cap)) if comparable else None,
            "excess": _money(max(ZERO, unit_amount - cap)) if comparable else None,
            "status": "insufficient_information" if missing else checked.get("status"),
            "reason": checked.get("reason"), "missing_fields": list(dict.fromkeys(missing)),
            "source_clauses": checked.get("source_clauses", []), "row_ids": checked.get("row_ids", []),
            "policy_profile": checked.get("policy_profile"),
            "transport_standard": checked.get("transport_standard"),
            "limit_unit": checked.get("unit"),
        }
        summary["items"].append(item)
    items = summary["items"]
    for output, field in (("cap_total", "cap"), ("standard_portion", "standard_portion"), ("excess", "excess")):
        if all(item[field] is not None for item in items):
            summary[output] = _money(sum((Decimal(item[field]) for item in items), ZERO))
    known = [Decimal(item["reference_amount"]) for item in items if item["reference_amount"] is not None]
    if known:
        summary["known_reference_amount"] = _money(sum(known, ZERO))
    mixed_payment = (facts.kind == "transport" and facts.transport_amount_scope != "fare_only")
    if mixed_payment and facts.amount is not None and facts.fare_amount is not None:
        summary["unassessed_amount"] = _money(facts.amount - facts.fare_amount)
        mixed_payment = facts.amount != facts.fare_amount
    if len(known) == len(items) and not mixed_payment:
        summary.update(status="estimated", reference_amount=_money(sum(known, ZERO)))
    elif known:
        summary["status"] = "partial"
    missing = list(dict.fromkeys(name for item in items for name in item["missing_fields"]))
    if any(item["status"] == "insufficient_information" for item in items):
        status = "insufficient_information"
    elif any(item["status"] == "manual_review" for item in items):
        status = "manual_review"
    elif any(item["status"] == "over_standard" for item in items):
        status = "over_standard"
    else:
        status = "within_checked_rules"
    if mixed_payment and not missing:
        status = "manual_review"
    return {
        "status": status, "reason": "expense_amount_projection", "approved": False,
        "missing_fields": missing, "estimate": summary,
        "source_clauses": list(dict.fromkeys(c for result in results for c in result.get("source_clauses", []))),
        "row_ids": list(dict.fromkeys(c for result in results for c in result.get("row_ids", []))),
    }



def _estimate_summary(facts, currency):
    return {
        "scenario": facts.expense_scenario, "kind": facts.kind,
        "currency": facts.currency, "policy_currency": currency,
        "status": "pending", "reference_amount": None, "known_reference_amount": None,
        "submitted_amount": None, "standard_portion": None, "excess": None,
        "cap_total": None, "items": [], "approved": False,
    }


def _estimate_pending(facts, summary, reason, *fields):
    return {"status": "insufficient_information", "reason": reason,
            "missing_fields": list(fields), "approved": False,
            "source_clauses": [], "estimate": summary}


def _estimate_allowance(facts, checker, *, max_days, currency, effective_from=None, effective_to=None):
    """Expand a natural-day allowance window and check every day with the same rule engine."""
    summary = _estimate_summary(facts, currency)
    end = facts.expense_end or facts.expense_date
    span = (end - facts.expense_date).days + 1
    if span > max_days:
        return _estimate_pending(facts, summary, "expense_period_too_long", "expense_end")
    dates = [facts.expense_date + timedelta(days=index) for index in range(span)]
    if any((effective_from and day < effective_from) or (effective_to and day > effective_to)
           for day in dates):
        return _estimate_pending(facts, summary, "expense_outside_published_period", "expense_date")
    transit_dates = set(facts.in_transit_dates or [])
    multiple = span > 1
    results = []
    for day in dates:
        if facts.in_transit_dates is not None:
            in_transit = day in transit_dates
        elif not multiple and facts.in_transit_day is not None:
            in_transit = facts.in_transit_day
        else:
            # A multi-day window cannot establish which days were spent travelling.
            in_transit = None
        unit = facts.model_copy(update={
            "expense_date": day, "expense_end": None, "in_transit_day": in_transit,
            "in_transit_dates": None, "amount": None, "amount_scope": "single",
            "check_out": None, "nightly_amounts": None, "fare_amount": None,
        })
        checked = checker(unit)
        results.append(checked)
        missing = list(checked.get("missing_fields", []))
        if (multiple and in_transit is None and facts.organizer_provides_meals is True
                and "in_transit_day" in missing):
            missing = [name for name in missing if name != "in_transit_day"] + ["in_transit_dates"]
        standard = Decimal(checked["amount"]) if checked.get("amount") is not None else None
        eligible = checked.get("status") == "within_checked_rules" and not missing and standard is not None
        summary["items"].append({
            "date": day.isoformat(), "amount": None, "cap": _money(standard),
            "reference_amount": _money(standard) if eligible else None,
            "standard_portion": None, "excess": None,
            "status": "insufficient_information" if missing else checked.get("status"),
            "reason": checked.get("reason"), "missing_fields": list(dict.fromkeys(missing)),
            "source_clauses": checked.get("source_clauses", []), "row_ids": checked.get("row_ids", []),
            "policy_profile": checked.get("policy_profile"),
            "transport_standard": checked.get("transport_standard"),
            "limit_unit": checked.get("unit"),
        })
    addon = _travel_addon(facts, checker)
    if addon is not None:
        summary["items"].append(addon)
    items = summary["items"]
    if all(item["cap"] is not None for item in items):
        summary["cap_total"] = _money(sum((Decimal(item["cap"]) for item in items), ZERO))
    known = [Decimal(item["reference_amount"]) for item in items if item["reference_amount"] is not None]
    if known:
        summary["known_reference_amount"] = _money(sum(known, ZERO))
    if len(known) == len(items):
        summary.update(status="estimated", reference_amount=_money(sum(known, ZERO)))
    elif known:
        summary["status"] = "partial"
    missing = list(dict.fromkeys(name for item in items for name in item["missing_fields"]))
    if any(item["status"] == "insufficient_information" for item in items):
        status = "insufficient_information"
    elif any(item["status"] == "manual_review" for item in items):
        status = "manual_review"
    elif any(item["status"] == "over_standard" for item in items):
        status = "over_standard"
    else:
        status = "within_checked_rules"
    return {
        "status": status, "reason": "expense_amount_projection", "approved": False,
        "missing_fields": missing, "estimate": summary,
        "source_clauses": list(dict.fromkeys(c for result in results for c in result.get("source_clauses", []))),
        "row_ids": list(dict.fromkeys(c for result in results for c in result.get("row_ids", []))),
    }




def _travel_addon(facts, checker):
    """One increment row per full 12 hours on a single ticket; never estimated or chained."""
    if facts.kind != "meal" or facts.departure_at is None or facts.arrival_at is None:
        return None
    hours = (facts.arrival_at - facts.departure_at).total_seconds() / 3600
    if hours <= 12:
        return None
    days = int(hours // 12)
    probe = facts.model_copy(update={
        "expense_date": facts.departure_at.date(), "expense_end": None,
        "in_transit_day": True, "in_transit_dates": None, "organizer_provides_meals": True,
        "departure_at": None, "arrival_at": None})
    checked = checker(probe)
    if checked.get("status") != "within_checked_rules" or checked.get("amount") is None:
        return None
    daily = Decimal(checked["amount"])
    if daily <= 0:
        return None
    amount = daily * days
    return {
        "date": facts.departure_at.date().isoformat(), "amount": None, "cap": _money(amount),
        "reference_amount": _money(amount), "standard_portion": None, "excess": None,
        "status": "within_checked_rules", "reason": "university_travel_12h_addon",
        "missing_fields": [], "source_clauses": checked.get("source_clauses", []), "row_ids": [],
        "policy_profile": checked.get("policy_profile"), "transport_standard": None,
        "limit_unit": "person_day", "addon": True,
        "travel_hours": f"{hours:g}", "addon_days": days,
    }


def bind_estimate_sources(result, sources, doc):
    """Each monetary conclusion needs real, same-version body/table evidence."""
    estimate = result.get("estimate")
    if not estimate:
        return
    real = {}
    for source in sources:
        if (isinstance(source.get("text"), str) and source["text"].strip() and source.get("chunk")
                and source.get("document_id") == doc["id"] and source.get("version") == doc["version"]
                and source.get("source_sha256") == doc["source_sha256"]
                and source.get("source_type") in {None, "original"}):
            real[source["chunk"]] = source
    failed = False
    for item in estimate["items"]:
        clause_ids = [key for key, source in real.items() if source.get("clause") in item["source_clauses"]]
        transport_tables = []
        matching_tables = []
        if estimate["kind"] == "transport" and item.get("policy_profile") == "university":
            for key in clause_ids:
                table = real[key].get("table") or {}
                headers, cells = table.get("headers", []), table.get("cells", [])
                if "标准" in headers and len(headers) == len(cells):
                    transport_tables.append(key)
                    if cells[headers.index("标准")] == item.get("transport_standard"):
                        matching_tables.append(key)
            clause_ids = [key for key in clause_ids if key not in transport_tables or key in matching_tables]
        seen_clauses = {real[key].get("clause") for key in clause_ids}
        complete = (bool(item["source_clauses"]) and set(item["source_clauses"]) <= seen_clauses
                    and set(item["row_ids"]) <= real.keys())
        if (estimate["kind"] == "transport" and item.get("policy_profile") == "university"
                and item["status"] in {"within_checked_rules", "over_standard"}):
            complete = complete and len(matching_tables) == 1
        item["evidence_ids"] = list(dict.fromkeys([*clause_ids, *(key for key in item["row_ids"] if key in real)]))
        item["primary_evidence_ids"] = item["row_ids"] or matching_tables or clause_ids
        item["evidence_verified"] = complete
        if not complete:
            failed = True
            for key in ("reference_amount", "standard_portion", "excess", "cap"):
                item[key] = None
            item.update(status="insufficient_information", reason="estimate_evidence_incomplete")
    result["sources"] = list(real.values())
    if failed:
        for key in ("reference_amount", "known_reference_amount", "standard_portion", "excess", "cap_total"):
            estimate[key] = None
        estimate["status"] = "pending"
        result.update(status="insufficient_information", reason="estimate_evidence_incomplete")


def _money(value):
    return str(value) if value is not None else None
