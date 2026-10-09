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
    if facts.academic_title in rules.title_categories:
        categories.add(rules.title_categories[facts.academic_title])
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


def check_university_expense(facts, rules: UniversityRules, lodging_rows: list[LodgingRow]) -> dict:
    clauses = list(dict.fromkeys(rules.source_clauses["scope"] + rules.source_clauses[facts.kind]))

    def result(status, reason, **extra):
        return {"status": status, "reason": reason, "source_clauses": clauses,
                "approved": False, "scope": "general_standard_only",
                "policy_profile": "university", **extra}

    def missing(*fields):
        return result("insufficient_information", "required_facts_missing", missing_fields=list(fields))

    if facts.mainland_business_trip is None:
        return missing("mainland_business_trip")
    if not facts.mainland_business_trip:
        return result("manual_review", "outside_domestic_business_scope")
    if facts.special_approval is True:
        return result("manual_review", "university_exception_conditions")
    if facts.kind == "transport":
        category = transport_category(facts, rules)
        if category not in rules.transport_classes:
            return missing("transport_standard")
        absent = [field for field in ("transport_mode", "seat_class") if not getattr(facts, field)]
        if absent:
            return missing(*absent)
        allowed = rules.transport_classes[category].get(facts.transport_mode, [])
        return result("within_checked_rules" if facts.seat_class in allowed else "over_standard",
                      "university_transport_standard", transport_standard=category,
                      allowed_classes=allowed)
    if facts.kind == "hotel":
        if facts.organizer_arranged_lodging is True:
            return result("manual_review", "university_conference_lodging_exception",
                          missing_fields=[] if facts.organizer_lodging_proof is True else ["organizer_lodging_proof"])
        if facts.trip_context in {"fieldwork", "noon_rest"}:
            return result("manual_review", "university_exception_conditions")
        limit = lookup_lodging(lodging_rows, destination=facts.destination,
                               province=facts.destination_province, standard=facts.lodging_standard,
                               day=facts.expense_date)
        if limit["status"] != "ok":
            return {**result(limit["status"], limit["reason"]), **limit}
        shared = {key: limit[key] for key in ("cap", "currency", "unit", "row_ids", "seasonal", "lodging_standard")}
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
    if facts.personnel_type == "student" and facts.student_activity_eligible is not True:
        return missing("student_activity_eligible")
    if facts.trip_context in {"self_drive", "rental", "fieldwork"} or facts.personnel_type == "invited":
        return result("manual_review", "university_exception_conditions")
    if facts.kind == "meal":
        if not facts.allowance_region:
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
        if facts.allowance_region not in {*rules.meal_regions, *rules.ordinary_meal_regions}:
            return missing("allowance_region")
        amount = rules.meal_regions.get(facts.allowance_region, rules.meal_daily)
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
