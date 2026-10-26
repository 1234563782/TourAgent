"""Validated public contracts; no provider credentials or employee identifiers."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueryFields(Contract):
    query_scope: Literal["hotel", "transport", "combined"] = Field(
        description="Requested components: hotel only, transport only, or combined transport and hotel. "
                    "Set from the user's goal, never infer from missing fields. "
                    "A scope is required before execution.")
    journey: Literal["one_way", "round_trip"] | None = Field(default=None,
        description="Explicit transport direction. Round-trip requests require a return date; "
                    "do not turn an unspecified return date into a one-way request.")
    origin: str | None = Field(default=None, min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    departure_date: date | None = None
    return_date: date | None = None
    check_in: date | None = None
    check_out: date | None = None
    purpose: str = Field(default="", max_length=2000)
    meeting_address: str = Field(default="", max_length=300)
    transport: Literal["flight", "train", "compare"] = "compare"
    train_service: Literal["any", "high_speed"] = "any"
    budget: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    employee_grade: str | None = None
    personnel_type: Literal["staff", "student", "invited"] | None = None
    student_activity_eligible: bool | None = Field(default=None,
        description="Explicit school teaching/research, contest, social practice or research-survey activity.")
    academic_title: str | None = Field(default=None, description="Explicit academic title, not a travel preference.")
    technical_grade: int | None = Field(default=None, ge=1, description="Explicit professional post grade.")
    management_grade: int | None = Field(default=None, ge=1)
    transport_standard: str | None = Field(default=None,
        description="Applicable transport category; explicit user value takes precedence, otherwise the active policy may derive it from identity.")
    lodging_standard: str | None = Field(default=None,
        description="Separate lodging identity. Never derived from transport category, title, or post grade.")
    destination_province: str | None = Field(default=None, max_length=100)
    mainland_business_trip: bool | None = None
    long_assignment_days: int | None = Field(default=None, ge=1,
        description="User-confirmed total trip or continuous assignment days, including short trips. Never infer from hotel nights.")
    special_approval: bool | None = Field(default=None,
        description="Whether special authorization has actually been granted for this trip. "
                    "A submitted application alone is not a granted authorization.")
    room_occupants: int | None = Field(default=None, ge=1,
        description="User-stated occupants per room, including children. A complete party in a single "
                    "room also establishes this count. Never average multiple rooms.")
    hotel_rooms: int | None = Field(default=None, ge=1, strict=True,
        description="Explicit total rooms, not guests.")
    hotel_adults: int | None = Field(default=None, ge=1, strict=True,
        description="Explicit total adults, not per-room occupants.")
    hotel_child_ages: list[Annotated[int, Field(strict=True, ge=0)]] | None = Field(default=None,
        description="Explicit child ages; [] means no children, null unknown.")
    roommate_grade: str | None = None
    voluntary_twin_share: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def active_query_fields(cls, values):
        if not isinstance(values, dict):
            return values
        payload = dict(values)
        scope = payload.get("query_scope", cls.model_fields["query_scope"].default)
        inactive = set()
        if scope == "hotel":
            inactive.update(("origin", "departure_date", "return_date", "journey", "transport", "train_service"))
        elif scope == "transport":
            inactive.update(("check_in", "check_out", "hotel_rooms", "hotel_adults", "hotel_child_ages"))
        if payload.get("journey") == "one_way":
            inactive.add("return_date")
        for field in inactive:
            payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def valid_dates(self):
        if self.journey == "round_trip" and not self.return_date and self.query_scope != "hotel":
            raise ValidationError.from_exception_data(type(self).__name__, [
                {"type": "missing", "loc": ("return_date",), "input": {}}])
        if self.return_date and self.departure_date and self.return_date < self.departure_date:
            raise ValueError("Return date must not precede departure")
        if bool(self.check_in) != bool(self.check_out):
            raise ValueError("Both hotel dates are required")
        if self.check_in and self.check_out <= self.check_in:
            raise ValueError("Checkout must be after check-in")
        for value in (self.origin, self.destination):
            if value is None:
                continue
            if not value.strip() or any(ord(char) < 32 for char in value):
                raise ValueError("City names must be nonempty and contain no control characters")
        return self

    def search_stages(self) -> list[tuple[str, bool]]:
        stages = []
        if self.query_scope != "hotel":
            kinds = ["train", "flight"] if self.transport == "compare" else [self.transport]
            stages.extend((kind, False) for kind in kinds)
            if self.return_date and self.journey != "one_way":
                stages.extend((kind, True) for kind in kinds)
        if self.query_scope == "hotel" or (self.query_scope != "transport" and self.check_in):
            stages.append(("hotel", False))
        return stages


class HotelRequest(QueryFields):
    query_scope: Literal["hotel"] = "hotel"
    check_in: date
    check_out: date


class TransportRequest(QueryFields):
    query_scope: Literal["transport"] = "transport"
    origin: str = Field(min_length=1, max_length=100)
    departure_date: date


class CombinedRequest(TransportRequest):
    query_scope: Literal["combined"] = "combined"
    check_in: date
    check_out: date


QueryRequest = HotelRequest | TransportRequest | CombinedRequest


def parse_query_request(values: dict, *, default_mainland_business_trip: bool = True,
                        default_single_guest: bool = True) -> QueryRequest:
    """Project reviewed facts into the requested components, preserving stored facts elsewhere."""
    values = dict(values)
    values.setdefault("mainland_business_trip", default_mainland_business_trip)
    scope = values.get("query_scope")
    party_fields = ("hotel_rooms", "hotel_adults", "hotel_child_ages")
    if (default_single_guest and isinstance(scope, str) and scope in {"hotel", "combined"}
            and values.get("room_occupants") in (None, 1)
            and all(values.get(name) is None for name in party_fields)):
        values.update(hotel_rooms=1, hotel_adults=1, hotel_child_ages=[], room_occupants=1)
    contracts = {"hotel": HotelRequest, "transport": TransportRequest, "combined": CombinedRequest}
    if not isinstance(scope, str) or scope not in contracts:
        raise ValidationError.from_exception_data("QueryRequest", [
            {"type": "missing" if scope is None else "literal_error", "loc": ("query_scope",),
             "input": scope, **({} if scope is None else {"ctx": {"expected": "'hotel', 'transport' or 'combined'"}})}])
    contract = contracts[scope]
    return contract.model_validate(values)


class Quote(Contract):
    amount: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = None
    basis: Literal["ticket", "night", "unknown"] = "unknown"
    status: Literal["indicative", "missing"] = "missing"
    taxes_confirmed: bool = False


class Candidate(Contract):
    id: str
    kind: Literal["flight", "train", "hotel"]
    label: str
    source: str = "flyai"
    observed_at: str
    request_date: date
    quote: Quote = Field(default_factory=Quote)
    departure: str | None = None
    arrival: str | None = None
    address: str | None = None
    url: str | None = None
    date_verified: bool = False
    inventory_verified: bool = False
    transport_mode: Literal["flight", "high_speed_train", "regular_train"] | None = None
    seat_class: str | None = None
    details: dict = Field(default_factory=dict)


class SearchResult(Contract):
    status: Literal["ok", "empty", "unavailable", "error"]
    candidates: list[Candidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None


class PolicyRules(Contract):
    version: str = Field(min_length=1)
    effective_from: date
    effective_to: date | None = None
    currency: str = "CNY"
    hotel_nightly_caps: dict[str, Decimal] = Field(default_factory=dict)
    transport_ticket_caps: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_rules(self):
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Policy effective period is invalid")
        for cap in [*self.hotel_nightly_caps.values(), *self.transport_ticket_caps.values()]:
            if not cap.is_finite() or cap <= 0:
                raise ValueError("Policy caps must be finite and positive")
        return self


def check_candidate(candidate: Candidate, trip: QueryRequest, rules: PolicyRules | None) -> dict:
    """A numeric cap comparison is not an approval or complete compliance check."""
    result = {"candidate_id": candidate.id, "status": "unverified", "reason": "policy_unconfigured"}
    if rules is None:
        return result
    result["policy_version"] = rules.version
    travel_dates = [value for value in (trip.departure_date, trip.return_date, trip.check_in, trip.check_out)
                    if value is not None]
    if min(travel_dates) < rules.effective_from or (rules.effective_to and max(travel_dates) > rules.effective_to):
        return {**result, "reason": "policy_outside_effective_period"}
    cap = (rules.hotel_nightly_caps.get(trip.destination) if candidate.kind == "hotel"
           else rules.transport_ticket_caps.get(candidate.kind))
    if cap is None:
        return {**result, "reason": "no_applicable_cap"}
    quote = candidate.quote
    if quote.amount is None or quote.currency != rules.currency:
        return {**result, "reason": "price_or_currency_unconfirmed"}
    expected_basis = "night" if candidate.kind == "hotel" else "ticket"
    if quote.basis != expected_basis:
        return {**result, "reason": "price_basis_unconfirmed"}
    if quote.amount > cap:
        return {**result, "status": "over_cap", "reason": "quoted_amount_exceeds_cap", "cap": str(cap)}
    return {**result, "reason": "indicative_amount_within_cap_not_final_approval", "cap": str(cap)}
