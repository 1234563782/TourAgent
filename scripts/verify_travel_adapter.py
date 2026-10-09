"""Opt-in live adapter smoke test; outputs only sanitized field summaries."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_travel.contracts import HotelRequest, TransportRequest
from business_travel.providers import FlyAIProvider
from business_travel.settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--kind", choices=["flight", "train", "hotel"], required=True)
    parser.add_argument("--origin")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--checkout")
    args = parser.parse_args()
    settings = Settings(api_key="", model="", base_url="", node_path=args.node, flyai_cli_path=args.cli,
                        storage_dir=Path("."), legacy_memory_dir=Path("."))
    try:
        trip = (HotelRequest(destination=args.destination, check_in=args.date, check_out=args.checkout)
                if args.kind == "hotel" else TransportRequest(
                    origin=args.origin, destination=args.destination, departure_date=args.date, transport=args.kind))
    except ValueError:
        print(json.dumps({"status": "error", "error_code": "invalid_probe_configuration_or_dates"}))
        return 2
    result = FlyAIProvider(settings).search(args.kind, trip)
    print(json.dumps({
        "kind": args.kind, "status": result.status, "count": len(result.candidates),
        "priced_count": sum(item.quote.amount is not None for item in result.candidates),
        "date_verified_count": sum(item.date_verified for item in result.candidates),
        "link_count": sum(item.url is not None for item in result.candidates),
        "warnings": result.warnings, "error_code": result.error_code,
    }, ensure_ascii=True, indent=2))
    return 0 if result.status in {"ok", "empty"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
