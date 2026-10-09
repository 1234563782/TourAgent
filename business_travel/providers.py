"""Read-only provider adapters with explicit unavailable and missing-price states."""

import hashlib
import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from .contracts import Candidate, Quote, SearchResult, QueryRequest
from .settings import Settings


def safe_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def numeric_price(value):
    if isinstance(value, bool) or not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", str(value)):
        return None
    return Decimal(str(value))


def normalize_flyai(payload: dict, kind: str, trip: QueryRequest, limit: int, *,
                    inbound: bool = False, currency: str | None = None) -> SearchResult:
    if not isinstance(payload, dict) or payload.get("status") != 0:
        return SearchResult(status="error", error_code="provider_rejected")
    items = payload.get("data", {}).get("itemList")
    if not isinstance(items, list):
        return SearchResult(status="error", error_code="invalid_provider_schema")
    observed = datetime.now(timezone.utc).isoformat()
    requested_date = (trip.return_date if inbound else trip.departure_date)
    if kind == "hotel":
        requested_date = trip.check_in
    if requested_date is None:
        return SearchResult(status="error", error_code="missing_search_date")
    candidates = []
    warnings = ["Prices are indicative; availability and booking conditions are not confirmed."]
    if "体验模式" in str(payload.get("systemMessage", "")):
        warnings.append("Provider trial mode: results may be restricted.")
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        amount = numeric_price(item.get("ticketPrice") if kind == "flight" else item.get("price"))
        pricing = Quote(amount=amount, currency=currency, status="indicative" if amount is not None else "missing",
                        basis="ticket" if kind in ("flight", "train") else "unknown",
                        taxes_confirmed=amount is not None)
        journeys = item.get("journeys") or []
        segments = [segment for journey in journeys if isinstance(journey, dict)
                    for segment in (journey.get("segments") or []) if isinstance(segment, dict)]
        first = segments[0] if segments else {}
        last = segments[-1] if segments else {}
        departure = first.get("depDateTime")
        arrival = last.get("arrDateTime")
        date_verified = isinstance(departure, str) and departure[:10] == requested_date.isoformat()
        if kind != "hotel" and not date_verified:
            warnings.append("Discarded a transport result with an unverified departure date.")
            continue
        details = ({"star": item.get("star"), "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"), "brand": item.get("brandName")}
                   if kind == "hotel" else {"segments": segments, "inbound": inbound})
        label = str(item.get("name") or " / ".join(
            str(segment.get("marketingTransportNo") or "") for segment in segments) or kind)
        candidate_id = hashlib.sha256(json.dumps(
            [kind, trip.model_dump(mode="json"), inbound, item], sort_keys=True, ensure_ascii=False
        ).encode()).hexdigest()[:20]
        candidates.append(Candidate(
            id=candidate_id, kind=kind, label=label, observed_at=observed,
            request_date=requested_date, quote=pricing, departure=departure, arrival=arrival,
            address=item.get("address"), url=safe_url(item.get("jumpUrl") or item.get("detailUrl")),
            date_verified=date_verified, details=details,
        ))
    return SearchResult(status="ok" if candidates else "empty", candidates=candidates, warnings=list(dict.fromkeys(warnings)))


class FlyAIProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def search(self, kind: str, trip: QueryRequest, inbound: bool = False) -> SearchResult:
        if kind not in {"flight", "train", "hotel"}:
            return SearchResult(status="error", error_code="unsupported_search")
        if (kind, inbound) not in trip.search_stages():
            return SearchResult(status="error", error_code="unrequested_search_component")
        config = self.settings
        if not config.node_path or not config.flyai_cli_path:
            return SearchResult(status="unavailable", error_code="flyai_runtime_unconfigured")
        if not Path(config.node_path).is_file() or not Path(config.flyai_cli_path).is_file():
            return SearchResult(status="unavailable", error_code="flyai_runtime_missing")
        args = [config.node_path, config.flyai_cli_path, f"search-{kind}"]
        if kind == "hotel":
            if not trip.check_in or not trip.check_out:
                return SearchResult(status="error", error_code="hotel_dates_required")
            args += ["--dest-name", trip.destination, "--check-in-date", trip.check_in.isoformat(),
                     "--check-out-date", trip.check_out.isoformat()]
        else:
            search_date = trip.return_date if inbound else trip.departure_date
            if not search_date:
                return SearchResult(status="error", error_code="return_date_required")
            origin, destination = ((trip.destination, trip.origin) if inbound else (trip.origin, trip.destination))
            args += ["--origin", origin, "--destination", destination, "--dep-date", search_date.isoformat()]
        # Do not pass the assistant's LLM credential to the vendor subprocess.
        env = {key: value for key, value in os.environ.items() if not key.startswith("TRAVEL_LLM_")}
        try:
            completed = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace",
                                       timeout=config.timeout, shell=False, env=env,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return SearchResult(status="error", error_code="provider_timeout")
        except OSError:
            return SearchResult(status="unavailable", error_code="provider_process_unavailable")
        if completed.returncode:
            return SearchResult(status="error", error_code="provider_process_failed")
        if len(completed.stdout) > 4 * 1024 * 1024:
            return SearchResult(status="error", error_code="provider_response_too_large")
        try:
            payload = json.loads(completed.stdout)
            return normalize_flyai(payload, kind, trip, config.max_candidates, inbound=inbound,
                                   currency=os.getenv("FLYAI_PRICE_CURRENCY") or None)
        except (ValueError, TypeError, AttributeError):
            return SearchResult(status="error", error_code="invalid_provider_response")


class InformationProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def weather(self, city: str, day: str) -> dict:
        try:
            date.fromisoformat(day)
            with httpx.Client(timeout=self.settings.timeout, follow_redirects=False) as client:
                response = client.get(f"{self.settings.weather_url.rstrip('/')}/{quote(city, safe='')}",
                                      params={"format": "j1"})
                response.raise_for_status()
                forecasts = response.json().get("weather", [])
            matching = [entry for entry in forecasts if entry.get("date") == day]
            return {"status": "ok" if matching else "unavailable", "source": "wttr.in",
                    "date": day, "forecasts": matching, "reason": None if matching else "date_outside_forecast"}
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            return {"status": "error", "reason": "weather_query_failed"}

    def commute(self, origin_address: str, destination_address: str, city: str) -> dict:
        if not self.settings.amap_key:
            return {"status": "unavailable", "reason": "AMAP_API_KEY_missing"}
        try:
            with httpx.Client(timeout=self.settings.timeout, follow_redirects=False) as client:
                locations = []
                for address in (origin_address, destination_address):
                    response = client.get(f"{self.settings.amap_url.rstrip('/')}/v3/geocode/geo",
                                          params={"key": self.settings.amap_key, "address": address, "city": city})
                    response.raise_for_status()
                    body = response.json()
                    geocodes = body.get("geocodes", [])
                    if body.get("status") != "1" or len(geocodes) != 1:
                        return {"status": "unavailable", "reason": "address_ambiguous_or_geocoding_failed"}
                    locations.append(geocodes[0]["location"])
                response = client.get(f"{self.settings.amap_url.rstrip('/')}/v3/direction/driving",
                                      params={"key": self.settings.amap_key, "origin": locations[0],
                                              "destination": locations[1], "extensions": "base"})
                response.raise_for_status()
                body = response.json()
            paths = body.get("route", {}).get("paths", [])
            if body.get("status") != "1" or not paths:
                return {"status": "unavailable", "reason": "route_not_found"}
            return {"status": "ok", "source": "amap", "mode": "driving",
                    "distance_meters": paths[0].get("distance"), "duration_seconds": paths[0].get("duration"),
                    "warning": "Current route estimate, not a guaranteed future commute duration."}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return {"status": "error", "reason": "commute_query_failed"}
