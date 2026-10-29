"""University general-standard checks driven by the published policy package."""

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract
from .policy_lodging import LodgingRow, lookup_lodging


class UniversityRules(Contract):
    schema_version: Literal[2] = 2
    profile: Literal["university"] = "university"
    currency: str = "CNY"
    source_clauses: dict[str, list[str]]
    transport_classes: dict[str, dict[str, list[str]]]
    category_order: list[str]
    title_categories: dict[str, str]
    senior_post_max_grade: int = Field(ge=1)
    senior_post_category: str
    student_category: str
    meal_daily: Decimal = Field(ge=0, allow_inf_nan=False)
    meal_regions: dict[str, Decimal]
    ordinary_meal_regions: list[str]
    local_transport_daily: Decimal = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_rules(self):
        for family in ("scope", "hotel", "transport", "meal", "local_transport"):
            if not self.source_clauses.get(family):
                raise ValueError("Missing source clauses")
        categories = set(self.transport_classes)
        if len(self.category_order) != len(categories) or set(self.category_order) != categories:
            raise ValueError("Transport categories require an explicit highest-first order")
        if set(self.ordinary_meal_regions) & set(self.meal_regions):
            raise ValueError("Conflicting meal regions")
        if not {self.senior_post_category, self.student_category, *self.title_categories.values()} <= categories:
            raise ValueError("Unknown transport category")
        if any(not amount.is_finite() or amount < 0 for amount in self.meal_regions.values()):
            raise ValueError("Invalid regional meal rate")
        return self


def transport_category(facts, rules):
    categories = set()
    academic_title = getattr(facts, "academic_title", None)
    if academic_title in rules.title_categories:
        categories.add(rules.title_categories[academic_title])
    if any(grade is not None and grade <= rules.senior_post_max_grade
           for grade in (facts.technical_grade, facts.management_grade)):
        categories.add(rules.senior_post_category)
    category = next((name for name in rules.category_order if name in categories), None)
    if facts.transport_standard and category and facts.transport_standard != category:
        return None
    category = facts.transport_standard or category
    if facts.personnel_type == "student":
        if facts.student_activity_eligible is not True:
            return None
        if category and category != rules.student_category:
            return None
        return rules.student_category
    return category


def lodging_standard_for(facts, rules):
    """Return only a separately stated lodging identity.

    Transport category, academic title, and post grade do not choose a lodging
    column. A category label such as 一类 is not a lodging-table header.
    """
    explicit = getattr(facts, "lodging_standard", None)
    if not explicit or explicit in set(rules.transport_classes) or explicit in rules.category_order:
        return None
    return explicit


def planning_standards_for(facts, rules):
    """Return transport identity and lodging identity separately."""
    return transport_category(facts, rules), lodging_standard_for(facts, rules)


def known_transport_classes(rules):
    """Policy vocabulary, distinct from the classes permitted for one category."""
    modes = dict.fromkeys(mode for group in rules.transport_classes.values() for mode in group)
    return {mode: sorted({seat for group in rules.transport_classes.values()
                          for seat in group.get(mode, [])}) for mode in modes}




def _known_mainland_destination(facts, rules):
    names = {facts.destination, facts.destination_province} - {None}
    known = set(rules.ordinary_meal_regions) | set(rules.meal_regions)
    categories = getattr(rules, "city_categories", None) or {}
    known.update(categories)
    known.update(getattr(rules, "city_aliases", None) or {})
    for cities in categories.values():
        known.update(cities)
    return bool(names & known)

def check_university_expense(facts, rules: UniversityRules, lodging_rows: list[LodgingRow],
                             *, projection: bool = False) -> dict:
    clauses = list(dict.fromkeys(rules.source_clauses["scope"] + rules.source_clauses[facts.kind]))

    def result(status, reason, **extra):
        return {"status": status, "reason": reason, "source_clauses": clauses,
                "approved": False, "scope": "general_standard_only",
                "policy_profile": "university", **extra}

    def missing(*fields):
        return result("insufficient_information", "required_facts_missing", missing_fields=list(fields))

    mainland = facts.mainland_business_trip
    if mainland is None and _known_mainland_destination(facts, rules):
        # A destination the policy already maps to a mainland region settles the trip scope.
        mainland = True
    if mainland is None:
        return missing("mainland_business_trip")
    if not mainland:
        return result("manual_review", "outside_domestic_business_scope")
    if facts.special_approval is True:
        return result("manual_review", "university_exception_conditions")
    if facts.personnel_type == "student" and facts.student_activity_eligible is not True:
        return missing("student_activity_eligible")
    if facts.kind == "transport":
        category = transport_category(facts, rules)
        if category not in rules.transport_classes:
            return missing("transport_standard")
        absent = [field for field in ("transport_mode", "seat_class") if not getattr(facts, field)]
        if absent:
            return missing(*absent)
        allowed = rules.transport_classes[category].get(facts.transport_mode, [])
        if facts.seat_class not in known_transport_classes(rules).get(facts.transport_mode, []):
            return result("insufficient_information", "transport_class_mapping_required",
                          transport_standard=category, allowed_classes=allowed)
        return result("within_checked_rules" if facts.seat_class in allowed else "over_standard",
                      "university_transport_standard", transport_standard=category,
                      allowed_classes=allowed)
    if facts.kind == "hotel":
        if facts.organizer_arranged_lodging is True:
            return result("manual_review", "university_conference_lodging_exception",
                          missing_fields=[] if facts.organizer_lodging_proof is True else ["organizer_lodging_proof"])
        if facts.trip_context in {"fieldwork", "noon_rest"}:
            return result("manual_review", "university_exception_conditions")
        category = transport_category(facts, rules)
        lodging_standard = lodging_standard_for(facts, rules)
        limit = lookup_lodging(lodging_rows, destination=facts.destination,
                               province=facts.destination_province, standard=lodging_standard,
                               day=facts.expense_date)
        if limit["status"] != "ok":
            return {**result(limit["status"], limit["reason"]), **limit}
        shared = {key: limit[key] for key in ("cap", "currency", "unit", "row_ids", "seasonal", "lodging_standard")}
        shared["transport_standard"] = category
        if projection:
            # A projection compares the stated nightly amount with the cap. Currency and tax
            # defaults are display assumptions only; an explicit foreign currency or an
            # explicit tax-exclusive amount still blocks the comparison.
            if facts.amount is None:
                return {**missing("amount"), **shared}
            if facts.currency is not None and facts.currency != rules.currency:
                return result("insufficient_information",
                              "university_foreign_currency_projection", **shared)
            if facts.taxes_confirmed is False:
                return result("insufficient_information",
                              "university_tax_exclusive_projection", **shared)
            basis = facts.basis
            if basis == "unknown" and getattr(facts, "amount_scope", None) == "uniform_nightly":
                # "每晚同价" is a nightly amount; the basis enum has no value for it.
                basis = "room_night"
            if basis not in {"person_day", "room_night"}:
                return {**missing("basis"), **shared}
            assumptions = ["date_not_compared_with_receipt"]
            if facts.currency is None:
                assumptions.insert(0, "currency_cny_assumed")
            if facts.taxes_confirmed is None:
                assumptions.append("tax_inclusive_assumed")
            cap = Decimal(limit["cap"])
            return result("over_standard" if facts.amount > cap else "within_checked_rules",
                          "university_lodging_cap", amount=str(facts.amount),
                          excess=str(max(Decimal(0), facts.amount - cap)),
                          assumptions=assumptions, **shared)
        absent = [key for key in ("amount", "currency", "date_verified", "taxes_confirmed")
                  if getattr(facts, key) is None]
        if absent:
            return {**missing(*absent), **shared}
        if facts.currency != rules.currency or not facts.date_verified or not facts.taxes_confirmed:
            return result("insufficient_information", "university_quote_basis_required", **shared)
        if facts.basis == "room_night" and facts.occupants != 1:
            return result("insufficient_information", "personal_lodging_amount_required", **shared)
        if facts.basis not in {"person_day", "room_night"}:
            return result("insufficient_information", "university_quote_basis_required", **shared)
        cap = Decimal(limit["cap"])
        return result("over_standard" if facts.amount > cap else "within_checked_rules",
                      "university_lodging_cap", amount=str(facts.amount),
                      excess=str(max(Decimal(0), facts.amount - cap)), **shared)
    if facts.trip_context is None:
        return missing("trip_context")
    if facts.personnel_type is None:
        return missing("personnel_type")
    if facts.trip_context in {"self_drive", "rental", "fieldwork"} or facts.personnel_type == "invited":
        return result("manual_review", "university_exception_conditions")
    if facts.kind == "meal":
        # The allowance region follows the destination unless a separate region is stated.
        region = facts.allowance_region or facts.destination_province or facts.destination
        if not region:
            return missing("allowance_region")
        if facts.trip_context in {"conference", "training"}:
            if facts.organizer_provides_meals is None:
                return missing("organizer_provides_meals")
            if facts.organizer_provides_meals:
                if facts.in_transit_day is None:
                    return missing("in_transit_day")
                if not facts.in_transit_day:
                    return result("within_checked_rules", "university_meeting_meal_excluded",
                                  amount="0", currency=rules.currency, unit="person_day")
        if region not in {*rules.meal_regions, *rules.ordinary_meal_regions}:
            return missing("allowance_region")
        amount = rules.meal_regions.get(region, rules.meal_daily)
        return result("within_checked_rules", "university_calendar_day_meal",
                      amount=str(amount), currency=rules.currency, unit="person_day")
    absent = [field for field in ("local_transport_method", "paid_by_other_organization")
              if getattr(facts, field) is None]
    if absent:
        return missing(*absent)
    if facts.paid_by_other_organization:
        return result("within_checked_rules", "university_no_duplicate_local_transport",
                      amount="0", currency=rules.currency, unit="person_day")
    if facts.local_transport_method == "receipts":
        return result("manual_review", "university_local_transport_receipts")
    return result("within_checked_rules", "university_local_transport_daily",
                  amount=str(rules.local_transport_daily), currency=rules.currency, unit="person_day")


def load_reviewed_rules(data):
    if data.get("schema_version") == 2:
        return UniversityRules.model_validate(data)
    from .policy_rules import ReviewedRules
    return ReviewedRules.model_validate(data)
