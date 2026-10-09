"""Deterministic checks of reviewed policy data, one expense day/night at a time."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract


class ExpenseFacts(Contract):
    kind: Literal["hotel", "transport", "meal", "local_transport"]
    expense_date: date = Field(description=
        "Actual or planned expense/stay date only. Policy publication dates or years are not expense dates. "
        "For explanations, leave unknown unless the user provides an actual business date.")
    destination: str = Field(min_length=1, max_length=100,
                            description="Expense city, required for every expense kind, including local transport.")
    employee_grade: str | None = Field(default=None,
        description="Exact complete user-stated grade name. Never abbreviate 普通员工 to 普通.")
    personnel_type: Literal["staff", "student", "invited"] | None = None
    academic_title: str | None = Field(default=None, description="Explicit academic title, not a preference.")
    technical_grade: int | None = Field(default=None, ge=1)
    management_grade: int | None = Field(default=None, ge=1)
    transport_standard: str | None = Field(default=None, description="Explicit transport category; never infer from lodging rank.")
    lodging_standard: str | None = Field(default=None, description="Explicit lodging table rank, not inferred from title/transport category.")
    destination_province: str | None = Field(default=None, max_length=100)
    trip_context: Literal["ordinary", "conference", "training", "research", "fieldwork",
                          "self_drive", "rental", "noon_rest"] | None = None
    student_activity_eligible: bool | None = None
    organizer_provides_meals: bool | None = None
    in_transit_day: bool | None = None
    local_transport_method: Literal["allowance", "receipts"] | None = None
    paid_by_other_organization: bool | None = None
    organizer_arranged_lodging: bool | None = Field(default=None,
        description="Conference/training lodging arranged by the organizer AND paid by the traveler.")
    organizer_lodging_proof: bool | None = None
    mainland_business_trip: bool | None = None
    long_assignment_days: int | None = Field(default=None, ge=1,
        description="User-confirmed total trip or continuous assignment days, including short trips. Never default to zero.")
    special_approval: bool | None = Field(default=None,
        description="Whether special authorization has actually been GRANTED for this expense. "
                    "True means an approved outcome, not merely an application or an ongoing process. "
                    "False means authorization is not currently granted. Null means unknown.")
    amount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False,
        description="Actual expense incurred, not a proposed allowance or amount in a question.")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$",
        description="ISO 4217 uppercase three-letter currency code, not a currency name or symbol. "
                    "Normalize only a currency explicitly identified by the user; preserve that original "
                    "statement as evidence. If the currency is ambiguous or unstated, leave null. "
                    "Never infer it from destination or perform currency conversion.")
    basis: Literal["room_night", "person_day", "ticket", "unknown"] = "unknown"
    taxes_confirmed: bool | None = None
    date_verified: bool | None = None
    occupants: int | None = Field(default=None, ge=1)
    roommate_grade: str | None = Field(default=None,
        description="Exact complete roommate grade, without abbreviation. Same grade only if explicitly stated.")
    voluntary_twin_share: bool | None = None
    transport_mode: Literal["flight", "high_speed_train", "regular_train"] | None = None
    seat_class: str | None = None
    suitable_direct_train_hours: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    flight_approval: bool | None = Field(default=None,
        description="Whether permission to fly has actually been granted, not merely requested.")
    overnight_trip: bool | None = None
    allowance_region: str | None = Field(default=None,
        description="Explicit meal allowance policy region, not a replacement for the expense destination city.")
    covered_meals: list[Literal["breakfast", "lunch", "dinner"]] | None = Field(default=None,
        description="Meals provided FREE by host/hotel, NOT self-paid meals. "
                    "If breakfast/lunch are free and dinner self-paid, use [breakfast,lunch].")
    local_business_travel: bool | None = None
    local_transport_coverage: Literal["all", "partial", "none"] | None = None
    self_paid_local_travel: bool | None = None
    local_car_cost_reimbursed: bool | None = None


class ReviewedRules(Contract):
    schema_version: int = 1
    currency: str = "CNY"
    city_categories: dict[str, list[str]]
    city_aliases: dict[str, str] = Field(default_factory=dict)
    other_city_category: str
    hotel_caps: dict[str, dict[str, Decimal]]
    shared_room_occupants: Literal[2]
    shared_room_multiplier: Decimal = Field(gt=0, allow_inf_nan=False)
    transport_classes: dict[str, dict[str, list[str]]]
    flight_approval_grades: list[str]
    railway_priority_hours: Decimal = Field(gt=0, allow_inf_nan=False)
    meal_overnight_base: Decimal = Field(ge=0, allow_inf_nan=False)
    meal_region_bases: dict[str, Decimal]
    meal_same_day_base: Decimal = Field(ge=0, allow_inf_nan=False)
    meal_deductions: dict[str, Decimal]
    local_transport_daily: Decimal = Field(ge=0, allow_inf_nan=False)
    long_assignment_threshold: int = Field(ge=1)
    source_clauses: dict[str, list[str]]

    @model_validator(mode="after")
    def valid_rules(self):
        if self.schema_version != 1:
            raise ValueError("Unsupported reviewed rules schema")
        cities = [city for group in self.city_categories.values() for city in group]
        if len(cities) != len(set(cities)):
            raise ValueError("Cities must have one unambiguous category")
        if set(self.meal_deductions) != {"breakfast", "lunch", "dinner"}:
            raise ValueError("All meal deduction rates are required")
        if any(not rate.is_finite() or not 0 <= rate <= 1 for rate in self.meal_deductions.values()):
            raise ValueError("Invalid meal deduction rates")
        if sum(self.meal_deductions.values()) > 1:
            raise ValueError("Meal deductions exceed the daily base")
        categories = set(self.city_categories) | {self.other_city_category}
        for caps in self.hotel_caps.values():
            if set(caps) != categories or any(not cap.is_finite() or cap <= 0 for cap in caps.values()):
                raise ValueError("Each grade requires positive caps for every category")
        if any(not rate.is_finite() or rate < 0 for rate in self.meal_region_bases.values()):
            raise ValueError("Invalid regional meal base")
        for kind in ("hotel", "transport", "meal", "local_transport", "scope"):
            if not self.source_clauses.get(kind):
                raise ValueError("Every rule family requires source clauses")
        return self


def check_expense(facts: ExpenseFacts, rules: ReviewedRules) -> dict:
    sources = list(dict.fromkeys(rules.source_clauses["scope"] + rules.source_clauses[facts.kind]))

    def result(status, reason, **values):
        return {"status": status, "reason": reason, "source_clauses": sources,
                "approved": False, "scope": "single_expense_day_or_night", **values}

    def missing(*names):
        return result("insufficient_information", "required_facts_missing", missing_fields=list(names))

    # Manual review is a safety exit, not permission to apply domestic limits.
    # An explicit special approval must not be hidden behind unrelated missing facts.
    if facts.special_approval is True:
        return result("manual_review", "verify_special_approval_scope")
    if facts.mainland_business_trip is None:
        return missing("mainland_business_trip")
    if facts.mainland_business_trip is False:
        return result("manual_review", "outside_domestic_business_scope")
    if facts.long_assignment_days is None:
        return missing("long_assignment_days")
    if facts.long_assignment_days >= rules.long_assignment_threshold:
        return result("manual_review", "long_assignment_requires_separate_plan")
    if facts.special_approval is None:
        return missing("special_approval")
    if facts.kind == "hotel":
        if facts.employee_grade not in rules.hotel_caps:
            return missing("employee_grade")
        if facts.occupants is None:
            return missing("occupants")
        if facts.occupants not in (1, rules.shared_room_occupants):
            return result("manual_review", "special_room_arrangement")
        city = rules.city_aliases.get(facts.destination, facts.destination)
        known_cities = {name for names in rules.city_categories.values() for name in names}
        if city.endswith("市") and city[:-1] in known_cities:
            city = city[:-1]
        category = next((key for key, cities in rules.city_categories.items() if city in cities), None)
        if category is None:
            # An unknown name may be a district of an A/B city; never silently classify it as C.
            return result("insufficient_information", "city_mapping_required")
        cap = rules.hotel_caps[facts.employee_grade][category]
        if facts.occupants == rules.shared_room_occupants:
            if facts.roommate_grade not in rules.hotel_caps or facts.voluntary_twin_share is None:
                return missing("roommate_grade", "voluntary_twin_share")
            if not facts.voluntary_twin_share:
                return result("manual_review", "shared_room_conditions_not_met")
            cap = max(cap, rules.hotel_caps[facts.roommate_grade][category]) * rules.shared_room_multiplier
        absent = [key for key in ("amount", "currency", "taxes_confirmed", "date_verified")
                  if getattr(facts, key) is None]
        if absent:
            return missing(*absent)
        if (facts.basis != "room_night" or not facts.taxes_confirmed or not facts.date_verified
                or facts.currency != rules.currency):
            return result("insufficient_information", "nightly_tax_inclusive_quote_required")
        return result("over_standard" if facts.amount > cap else "within_checked_rules",
                      "hotel_nightly_cap_comparison", cap=str(cap), amount=str(facts.amount),
                      excess=str(max(Decimal(0), facts.amount - cap)),
                      currency=rules.currency, unit="room_night")
    if facts.kind == "transport":
        if facts.employee_grade not in rules.transport_classes:
            return missing("employee_grade")
        if not facts.transport_mode or not facts.seat_class:
            return missing("transport_mode", "seat_class")
        allowed = rules.transport_classes[facts.employee_grade].get(facts.transport_mode, [])
        if facts.seat_class not in allowed:
            return result("over_standard", "transport_class_requires_special_approval")
        if facts.transport_mode == "flight":
            if facts.employee_grade in rules.flight_approval_grades and facts.flight_approval is not True:
                return result("manual_review", "explicit_flight_approval_required")
            if facts.suitable_direct_train_hours is None:
                return missing("suitable_direct_train_hours")
            if facts.suitable_direct_train_hours <= rules.railway_priority_hours:
                return result("manual_review", "railway_preferred_verify_flight_exception")
        return result("within_checked_rules", "transport_class_checked_not_budget_or_approval")
    if facts.kind == "meal":
        if facts.overnight_trip is None or facts.covered_meals is None:
            return missing("overnight_trip", "covered_meals")
        if facts.overnight_trip and not facts.allowance_region:
            return missing("allowance_region")
        if facts.overnight_trip and facts.allowance_region not in rules.meal_region_bases:
            return result("insufficient_information", "allowance_region_mapping_required")
        base = (rules.meal_region_bases.get(facts.allowance_region, rules.meal_overnight_base)
                if facts.overnight_trip else rules.meal_same_day_base)
        deduction = sum((rules.meal_deductions[meal] for meal in set(facts.covered_meals)), Decimal(0))
        return result("within_checked_rules", "daily_meal_calculation",
                      amount=str(base * (1 - deduction)), base=str(base), currency=rules.currency)
    if facts.local_business_travel is None:
        return missing("local_business_travel")
    if facts.local_business_travel is False:
        amount = Decimal(0)
    elif facts.local_transport_coverage == "all" or facts.local_car_cost_reimbursed is True:
        amount = Decimal(0)
    elif facts.local_transport_coverage is None or facts.local_car_cost_reimbursed is None:
        return missing("local_transport_coverage", "local_car_cost_reimbursed")
    elif facts.self_paid_local_travel is None:
        return missing("self_paid_local_travel")
    else:
        amount = rules.local_transport_daily if facts.self_paid_local_travel else Decimal(0)
    return result("within_checked_rules", "daily_local_transport_calculation",
                  amount=str(amount), currency=rules.currency)
