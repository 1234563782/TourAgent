"""Explicit read-only Tuniu probe; no model required and no raw data printed."""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_travel.contracts import HotelRequest, TransportRequest
from business_travel.settings import Settings
from business_travel.tuniu import TuniuProvider


def main():
    parser = argparse.ArgumentParser(description="Run one authorized Tuniu read-only search")
    parser.add_argument("--kind", choices=["flight", "train", "hotel"], required=True)
    parser.add_argument("--origin")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--checkout")
    args = parser.parse_args()
    try:
        config = Settings.from_env()
        trip = (HotelRequest(destination=args.destination, check_in=args.date, check_out=args.checkout)
                if args.kind == "hotel" else TransportRequest(
                    origin=args.origin, destination=args.destination, departure_date=args.date, transport=args.kind))
        if date.fromisoformat(args.date) < date.today():
            raise ValueError("Date is in the past")
        if args.kind == "hotel" and not trip.check_out:
            raise ValueError("Checkout is required")
    except ValueError:
        print(json.dumps({"status": "error", "error_code": "invalid_probe_configuration_or_dates"}))
        return 2
    result = TuniuProvider(config).search(args.kind, trip)
    print(json.dumps({
        "provider": "tuniu", "kind": args.kind, "status": result.status,
        "candidate_count": len(result.candidates),
        "priced_count": sum(c.quote.amount is not None for c in result.candidates),
        "date_verified_count": sum(c.date_verified for c in result.candidates),
        "hotel_detail": [{
            "status": c.details.get("hotel_detail", {}).get("status"),
            "error_code": c.details.get("hotel_detail", {}).get("error_code"),
            "availability": c.details.get("hotel_detail", {}).get("availability"),
            "rate_count": len(c.details.get("hotel_detail", {}).get("rates", [])),
        } for c in result.candidates if c.kind == "hotel"],
        "error_code": result.error_code,
    }, indent=2))
    return 0 if result.status in {"ok", "empty"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
