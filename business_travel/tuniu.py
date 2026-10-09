"""Read-only official Tuniu remote MCP adapter; credentials never enter graph state."""

import asyncio
import hashlib
import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Candidate, Quote, SearchResult, QueryRequest
from .providers import numeric_price
from .settings import Settings


SEARCH_TOOLS = {
    "flight": "searchLowestPriceFlight",
    "train": "searchLowestPriceTrain",
    "hotel": "tuniuHotelSearch",
}
HOTEL_DETAIL_TOOL = "tuniuHotelDetail"
MAX_RESULT_BYTES = 4 * 1024 * 1024
DETAIL_TEXT_LIMIT = 500
_ACTIVE_QUERY = ContextVar("tuniu_active_query", default=False)
TRAIN_SEATS = {
    "gjrw": "高级软卧", "rw": "软卧", "rz": "软座", "swz": "商务座",
    "tdz": "特等座", "wz": "无座", "yw": "硬卧", "yz": "硬座",
    "edz": "二等座", "ydz": "一等座", "dw": "动卧", "ydw": "一等卧", "edw": "二等卧",
}


class ProviderFailure(Exception):
    """Only a static error code may cross the provider boundary."""


class _TransportLogFilter(logging.Filter):
    def filter(self, record):
        if _ACTIVE_QUERY.get():
            # SDK diagnostics can contain raw session IDs, vendor payloads and exception text.
            record.msg = "Tuniu MCP transport event; consult the sanitized provider result."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


logging.getLogger("mcp.client.streamable_http").addFilter(_TransportLogFilter())


def validate_endpoint(url: str, kind: str, auth_type: str = "oauth"):
    if auth_type not in {"oauth", "apiKey"} or kind not in SEARCH_TOOLS:
        raise ProviderFailure("tuniu_invalid_auth_or_service")
    prefix = "/mcp" if auth_type == "apiKey" else "/hybrid/mcp"
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "openapi.tuniu.cn"
            or parsed.path != f"{prefix}/{kind}" or parsed.query or parsed.fragment):
        raise ProviderFailure("tuniu_endpoint_not_allowed")


def search_arguments(kind: str, trip: QueryRequest, inbound: bool) -> dict:
    if kind not in SEARCH_TOOLS:
        raise ProviderFailure("unsupported_search")
    if (kind, inbound) not in trip.search_stages():
        raise ProviderFailure("unrequested_search_component")
    if kind == "hotel":
        if inbound or not trip.check_in or not trip.check_out:
            raise ProviderFailure("hotel_dates_required")
        return {"cityName": trip.destination, "checkIn": trip.check_in.isoformat(),
                "checkOut": trip.check_out.isoformat(), **hotel_occupancy_arguments(trip)}
    day = trip.return_date if inbound else trip.departure_date
    if day is None:
        raise ProviderFailure("return_date_required")
    origin, destination = ((trip.destination, trip.origin) if inbound else (trip.origin, trip.destination))
    return {"departureCityName": origin, "arrivalCityName": destination, "departureDate": day.isoformat()}


def hotel_occupancy_arguments(trip: QueryRequest, *, detail: bool = False) -> dict:
    arguments = {}
    if trip.hotel_adults is not None:
        arguments["adultNum"] = trip.hotel_adults
    if trip.hotel_child_ages is not None:
        arguments.update(childNum=len(trip.hotel_child_ages), childAges=trip.hotel_child_ages)
    if detail and trip.hotel_rooms is not None:
        arguments["roomNum"] = trip.hotel_rooms
    return arguments


def hotel_detail_arguments(candidate: Candidate, trip: QueryRequest) -> dict:
    hotel_id = candidate.details.get("hotelId")
    if candidate.kind != "hotel" or type(hotel_id) is not int or hotel_id <= 0:
        raise ProviderFailure("tuniu_invalid_hotel_id")
    search_arguments("hotel", trip, False)
    return {"hotelId": hotel_id, "checkIn": trip.check_in.isoformat(),
            "checkOut": trip.check_out.isoformat(), **hotel_occupancy_arguments(trip, detail=True)}


def decode_result(result, *, detail: bool = False) -> dict:
    if result.isError:
        raise ProviderFailure("tuniu_tool_failed")
    payload = result.structuredContent
    if payload is None:
        texts = [part.text for part in result.content if part.type == "text"]
        if len(texts) != 1 or len(texts[0].encode("utf-8")) > MAX_RESULT_BYTES:
            raise ProviderFailure("tuniu_invalid_response")
        payload = json.loads(texts[0])
    if not isinstance(payload, dict):
        raise ProviderFailure("tuniu_invalid_response")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ProviderFailure("tuniu_response_too_large")
    # Some MCP servers wrap the business JSON in a single string result.
    # Unwrap only that envelope; never override an explicit business failure.
    if set(payload) == {"result"} and isinstance(payload["result"], str):
        try:
            payload = json.loads(payload["result"])
        except ValueError:
            raise ProviderFailure("tuniu_invalid_response") from None
        if not isinstance(payload, dict) or not (
                {"successCode", "success"}.intersection(payload)
                or detail and {"hotelId", "roomTypes"}.issubset(payload)):
            raise ProviderFailure("tuniu_invalid_response")
    return payload


def _contains_reference(value):
    if isinstance(value, dict):
        return any(key in {"$ref", "$dynamicRef"} or _contains_reference(item) for key, item in value.items())
    return isinstance(value, list) and any(_contains_reference(item) for item in value)


async def call_search(url: str, token: str, kind: str, arguments: dict, timeout: float,
                      auth_type: str = "oauth") -> dict:
    active = _ACTIVE_QUERY.set(True)
    try:
        return await _call_search(url, token, kind, arguments, timeout, auth_type)
    finally:
        _ACTIVE_QUERY.reset(active)


async def _call_search(url: str, token: str, kind: str, arguments: dict, timeout: float,
                       auth_type: str = "oauth", *, detail: bool = False) -> dict:
    validate_endpoint(url, kind, auth_type)
    if detail and kind != "hotel":
        raise ProviderFailure("unsupported_search")
    tool_name = HOTEL_DETAIL_TOOL if detail else SEARCH_TOOLS[kind]
    headers = {"apiKey": token} if auth_type == "apiKey" else {"Authorization": f"Bearer {token}"}
    # No proxy inherited from the process, no redirects, no auto retry or OAuth side effects.
    async with httpx.AsyncClient(headers=headers,
                                 timeout=timeout, follow_redirects=False, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                matching = [tool for tool in listed.tools if tool.name == tool_name]
                if len(matching) != 1:
                    raise ProviderFailure("tuniu_detail_tool_unavailable" if detail
                                          else "tuniu_search_tool_unavailable")
                schema = matching[0].inputSchema
                # Reject references rather than letting an untrusted schema resolve network URLs.
                if _contains_reference(schema) or _contains_reference(matching[0].outputSchema):
                    raise ProviderFailure("tuniu_unsupported_tool_schema")
                Draft202012Validator.check_schema(schema)
                if not Draft202012Validator(schema).is_valid(arguments):
                    raise ProviderFailure("tuniu_tool_schema_mismatch")
                result = await session.call_tool(tool_name, arguments)
                return decode_result(result, detail=detail)


async def call_hotel_detail(url: str, token: str, arguments: dict, timeout: float,
                            auth_type: str = "oauth") -> dict:
    active = _ACTIVE_QUERY.set(True)
    try:
        return await _call_search(url, token, "hotel", arguments, timeout, auth_type, detail=True)
    finally:
        _ACTIVE_QUERY.reset(active)


def normalize_hotel_detail(payload: dict, arguments: dict, rate_limit: int) -> dict:
    """Keep a bounded data whitelist, never vendor booking tokens or display instructions."""
    if any(key in payload and payload[key] is not True for key in ("success", "successCode")):
        raise ProviderFailure("tuniu_provider_rejected")
    if type(payload.get("hotelId")) is not int or payload["hotelId"] != arguments["hotelId"]:
        raise ProviderFailure("tuniu_hotel_identity_mismatch")
    rooms = payload.get("roomTypes")
    if not isinstance(rooms, list):
        raise ProviderFailure("tuniu_invalid_detail_response")
    rates = []
    incomplete = False
    for room in rooms:
        if not isinstance(room, dict) or not isinstance(room.get("ratePlans"), list):
            incomplete = True
            continue
        if not room["ratePlans"]:
            incomplete = True
        for rate in room["ratePlans"]:
            if not isinstance(rate, dict):
                incomplete = True
                continue
            count = rate.get("count")
            count = count if type(count) is int and count >= 0 else None
            price = numeric_price(rate.get("rmbPrices"))
            text_fields = {
                "room_name": room.get("roomTypeName"), "bed_type": room.get("bedType"),
                "rate_name": rate.get("ratePlanName"), "meal": rate.get("mealText"),
                "cancellation": rate.get("cancelDesc"),
            }
            rates.append({
                **{key: value[:DETAIL_TEXT_LIMIT] if isinstance(value, str) else None
                   for key, value in text_fields.items()},
                "truncated_fields": [key for key, value in text_fields.items()
                                     if isinstance(value, str) and len(value) > DETAIL_TEXT_LIMIT],
                "amount": str(price) if price is not None else None,
                "currency": "CNY", "basis": "unknown",
                "taxes_confirmed": price is not None,
                "remaining_count": count,
                "availability": "unknown" if count is None else "sold_out" if count == 0 else "available_snapshot",
            })
    availability = ("available_snapshot" if any(rate["remaining_count"] for rate in rates)
                    else "sold_out" if rates and not incomplete
                    and all(rate["remaining_count"] == 0 for rate in rates) else "unknown")
    rank = {"available_snapshot": 0, "unknown": 1, "sold_out": 2}
    rates.sort(key=lambda rate: rank[rate["availability"]])
    return {
        "status": "ok", "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested": arguments, "date_verified": False, "inventory_verified": False,
        "supplier_default_fields": [key for key in ("roomNum", "adultNum", "childNum")
                                    if key not in arguments],
        "availability": availability, "rates": rates[:rate_limit],
        "returned_rate_count": len(rates), "rates_truncated": len(rates) > rate_limit,
        "incomplete_response": incomplete,
    }


async def enrich_hotels(result: SearchResult, trip: QueryRequest, settings: Settings,
                         url: str, token: str) -> SearchResult:
    deadline = asyncio.get_running_loop().time() + settings.tuniu_hotel_detail_budget
    stop_code = None
    for index, candidate in enumerate(result.candidates):
        detail = {"status": "not_queried", "reason": "detail_limit"}
        candidate.details["hotel_detail"] = detail
        if index >= settings.tuniu_hotel_detail_limit:
            continue
        if stop_code:
            detail["reason"] = stop_code
            continue
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            detail["reason"] = "detail_budget_exhausted"
            continue
        try:
            arguments = hotel_detail_arguments(candidate, trip)
            timeout = min(remaining, settings.tuniu_hotel_detail_timeout)
            async with asyncio.timeout(timeout):
                payload = await call_hotel_detail(url, token, arguments, timeout, settings.tuniu_auth_type)
            candidate.details["hotel_detail"] = normalize_hotel_detail(
                payload, arguments, settings.tuniu_hotel_rate_limit)
        except Exception as error:
            code = _error_code(error)
            candidate.details["hotel_detail"] = {"status": "error", "error_code": code}
            if code in {"tuniu_authorization_required", "tuniu_permission_denied", "tuniu_rate_limited",
                        "tuniu_detail_tool_unavailable", "tuniu_tool_schema_mismatch",
                        "tuniu_unsupported_tool_schema"}:
                stop_code = code
    return result


def _text(value):
    return value if isinstance(value, str) else None


def _timestamp(value):
    if not isinstance(value, str) or len(value) < 16:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalize_tuniu(payload: dict, kind: str, trip: QueryRequest, limit: int, *,
                    inbound: bool = False) -> SearchResult:
    from .plan_checks import check_timeline

    search_arguments(kind, trip, inbound)
    success = "success" if kind == "hotel" else "successCode"
    if payload.get(success) is not True:
        return SearchResult(status="error", error_code="tuniu_provider_rejected")
    items = payload.get("hotels" if kind == "hotel" else "data")
    if not isinstance(items, list):
        return SearchResult(status="error", error_code="tuniu_invalid_response")
    day = trip.check_in if kind == "hotel" else trip.return_date if inbound else trip.departure_date
    observed = datetime.now(timezone.utc).isoformat()
    warnings = ["First page only; indicative prices, not booking or policy approval.",
                "Availability has not been revalidated."]
    if kind == "hotel":
        warnings.append("Hotel starting price: nightly basis, occupancy and date inventory are unverified.")
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            warnings.append("Discarded a malformed result.")
            continue
        departure = _timestamp(item.get("departureTime"))
        if kind != "hotel" and (departure is None or departure.date() != day):
            warnings.append("Discarded a transport result with an unverified departure date.")
            continue
        details = {"inbound": inbound}
        if kind == "flight":
            base, tax = numeric_price(item.get("basePrice")), numeric_price(item.get("totalTax"))
            amount = base + tax if base is not None and tax is not None else None
            if amount is None:
                warnings.append("Flight total unknown: base fare and taxes are both required.")
            details.update({key: item.get(key) for key in (
                "airlineCompany", "departureAirport", "arrivalAirport", "cabinClass", "remainingSeats")})
            details.update(base_price=str(base) if base is not None else None,
                           tax=str(tax) if tax is not None else None)
            variants = [(_text(item.get("flightNumber")), amount, details)]
        elif kind == "train":
            train = _text(item.get("trainNum"))
            if trip.train_service == "high_speed" and not (
                    train and train.startswith(("G", "D", "C")) and train[1:2].isdigit()):
                continue
            prices, seats = item.get("price"), item.get("seatAvailable")
            if not isinstance(prices, dict) or not isinstance(seats, dict):
                warnings.append("Discarded a train result with malformed seat pricing.")
                continue
            details.update({key: item.get(key) for key in ("departStationName", "destStationName")})
            variants = []
            for code, label in TRAIN_SEATS.items():
                raw, available = prices.get(code + "Price"), seats.get(code + "Num")
                if raw in (None, "") and not (
                        isinstance(available, (int, float)) and not isinstance(available, bool) and available > 0):
                    continue
                if isinstance(available, (int, float)) and not isinstance(available, bool) and available <= 0:
                    continue
                train = _text(item.get("trainNum"))
                variants.append((f"{train} / {label}" if train else None, numeric_price(raw),
                                 {**details, "seat_type": label, "remaining_seats": available}))
        else:
            details.update({key: item.get(key) for key in (
                "hotelId", "starName", "brandName", "meal", "refund", "roomName", "cityName")})
            details.update(check_in=trip.check_in.isoformat(), check_out=trip.check_out.isoformat(),
                           price_type="starting_price")
            variants = [(_text(item.get("hotelName")), numeric_price(item.get("lowestPrice")), details)]
        for label, amount, detail in variants:
            if not label:
                warnings.append("Discarded an unnamed result.")
                continue
            quote = Quote(amount=amount, currency="CNY", basis="unknown" if kind == "hotel" else "ticket",
                          status="missing" if amount is None else "indicative",
                          taxes_confirmed=amount is not None)
            identity = json.dumps([kind, trip.model_dump(mode="json"), label, detail,
                                   str(amount), item.get("departureTime"), item.get("arrivalTime")],
                                  sort_keys=True, ensure_ascii=False)
            mode, seat_class = None, None
            if kind == "flight":
                mode = "flight"
                # Unmapped booking-class codes are not cabin classes.
                cabin = _text(item.get("cabinClass"))
                seat_class = cabin if cabin in {"经济舱", "超级经济舱", "公务舱", "商务舱", "头等舱"} else None
            elif kind == "train":
                number = _text(item.get("trainNum")) or ""
                if re.fullmatch(r"[GDC]\d+", number):
                    mode = "high_speed_train"
                elif re.fullmatch(r"(?:[ZTKY]|L)?\d+", number):
                    mode = "regular_train"
                seat_class = detail["seat_type"]
            candidates.append(Candidate(
                id=hashlib.sha256(identity.encode()).hexdigest()[:20], source="tuniu", kind=kind,
                label=label, request_date=day, observed_at=observed, quote=quote,
                departure=_text(item.get("departureTime")), arrival=_text(item.get("arrivalTime")),
                address=_text(item.get("address")), date_verified=kind != "hotel", details=detail,
                transport_mode=mode, seat_class=seat_class,
            ))
    # Scan the returned page before truncation: early seat variants must not hide
    # a later train that actually fits the requested hotel/return dates.
    if kind != "hotel":
        rank = {"consistent": 0, "unverified": 1, "conflict": 2}
        candidates.sort(key=lambda candidate: rank[check_timeline([candidate], trip)["status"]])
    candidates = candidates[:max(0, limit)]
    # Provider displayHint/message fields are deliberately not instructions for the model.
    return SearchResult(status="ok" if candidates else "empty", candidates=candidates,
                        warnings=list(dict.fromkeys(warnings)))


def _error_code(error: Exception) -> str:
    if isinstance(error, BaseExceptionGroup):
        codes = [_error_code(child) for child in error.exceptions]
        return next((code for code in codes if code != "tuniu_query_failed"), "tuniu_query_failed")
    if isinstance(error, ProviderFailure):
        return str(error)
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "tuniu_timeout"
    if isinstance(error, httpx.HTTPStatusError):
        return {401: "tuniu_authorization_required", 403: "tuniu_permission_denied",
                429: "tuniu_rate_limited"}.get(error.response.status_code, "tuniu_http_error")
    return "tuniu_query_failed"


class TuniuProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def search(self, kind: str, trip: QueryRequest, inbound: bool = False) -> SearchResult:
        try:
            arguments = search_arguments(kind, trip, inbound)
            url = getattr(self.settings, f"tuniu_{kind}_url")
            validate_endpoint(url, kind, self.settings.tuniu_auth_type)
            token = self.settings.tuniu_credential(kind)
            if not token:
                return SearchResult(status="unavailable", error_code="tuniu_authorization_required")
            if any(ord(char) < 33 or ord(char) > 126 for char in token):
                return SearchResult(status="unavailable", error_code="tuniu_invalid_token_format")
            # The existing synchronous LangGraph tools run this adapter in a worker thread.
            async def query():
                async with asyncio.timeout(self.settings.timeout):
                    payload = await call_search(url, token, kind, arguments, self.settings.timeout,
                                                self.settings.tuniu_auth_type)
                result = normalize_tuniu(payload, kind, trip, self.settings.max_candidates, inbound=inbound)
                if kind == "hotel" and result.status == "ok":
                    await enrich_hotels(result, trip, self.settings, url, token)
                return result
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                return SearchResult(status="error", error_code="tuniu_requires_sync_worker")
            return asyncio.run(query())
        except Exception as error:
            # Raw SDK/network exceptions can include headers or vendor response bodies.
            return SearchResult(status="error", error_code=_error_code(error))
