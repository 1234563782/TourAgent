"""Unit tests for hotel candidate tiered selection and selection pool filtering."""

import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from business_travel.contracts import Candidate, Quote
from business_travel.planning import (
    _select_top_hotel_candidates,
    _selection_candidates,
    _hotel_inventory_state,
)


def make_hotel(cid, amount, availability="unknown", address="合肥市"):
    return Candidate(
        id=cid,
        kind="hotel",
        label=f"Hotel_{cid}",
        observed_at="2026-11-01T10:00:00Z",
        request_date=date(2026, 11, 1),
        quote=Quote(amount=Decimal(str(amount)), currency="CNY"),
        address=address,
        details={
            "hotel_detail": {
                "availability": availability,
                "status": "ok",
            }
        },
    )


class HotelSelectionFunnelTests(unittest.TestCase):
    def test_hotel_inventory_state_handles_non_dict_and_none(self):
        # 1. Non-hotel kind
        non_hotel = Candidate(
            id="T1",
            kind="train",
            label="G1",
            observed_at="2026-11-01T10:00:00Z",
            request_date=date(2026, 11, 1),
            quote=Quote(amount=Decimal("100"), currency="CNY"),
        )
        self.assertIsNone(_hotel_inventory_state(non_hotel))

        # 2. None detail
        hotel_none = Candidate(
            id="H1",
            kind="hotel",
            label="H1",
            observed_at="2026-11-01T10:00:00Z",
            request_date=date(2026, 11, 1),
            quote=Quote(amount=Decimal("200"), currency="CNY"),
            details={},
        )
        self.assertEqual(_hotel_inventory_state(hotel_none), "unknown")

        # 3. Malformed hotel_detail
        hotel_malformed = Candidate(
            id="H2",
            kind="hotel",
            label="H2",
            observed_at="2026-11-01T10:00:00Z",
            request_date=date(2026, 11, 1),
            quote=Quote(amount=Decimal("200"), currency="CNY"),
            details={"hotel_detail": "not-a-dict"},
        )
        self.assertEqual(_hotel_inventory_state(hotel_malformed), "unknown")

        # 4. Sold out hotel state
        hotel_sold_out = make_hotel("H3", 200, availability="sold_out")
        self.assertEqual(_hotel_inventory_state(hotel_sold_out), "sold_out")

    def test_within_standard_unknown_not_evicted_by_over_standard_available_snapshot(self):
        """Tiered selection ensures within_standard candidates survive truncation."""
        # Cap is 400.
        profile = {"status": "ok", "lodging_cap": Decimal("400"), "lodging_currency": "CNY"}

        # 3 over_standard hotels with available_snapshot
        h_over1 = make_hotel("over1", 800, availability="available_snapshot")
        h_over2 = make_hotel("over2", 900, availability="available_snapshot")
        h_over3 = make_hotel("over3", 1000, availability="available_snapshot")

        # 2 within_standard hotels with unknown inventory
        h_within1 = make_hotel("win1", 300, availability="unknown")
        h_within2 = make_hotel("win2", 350, availability="unknown")

        candidates = [h_over1, h_over2, h_over3, h_within1, h_within2]

        # max_count = 2: under old logic, over1 and over2 would win due to availability_rank (0 vs 1).
        # Under new tiered logic, within_standard must win!
        selected = _select_top_hotel_candidates(candidates, profile, max_count=2)
        selected_ids = [c.id for c in selected]

        self.assertEqual(selected_ids, ["win1", "win2"])

    def test_selection_candidates_allows_within_standard_unknown_when_available_are_all_over_standard(self):
        """When available hotels are all over-standard, allow within_standard unknown hotels into selection pool."""
        h_over_avail = make_hotel("over_avail", 800, availability="available")
        h_win_unknown = make_hotel("win_unk", 350, availability="unknown")

        candidates = [h_over_avail, h_win_unknown]
        checks = {
            "over_avail": {"status": "over_standard", "cap": "400", "amount": "800", "currency": "CNY", "excess": "400"},
            "win_unk": {"status": "within_standard", "cap": "400", "amount": "350", "currency": "CNY", "excess": "0"},
        }
        required = {"hotel"}
        selectable_hotel_ids = {"over_avail"}

        # Because over_avail is over_standard, win_unk (within_standard) must be prioritized and entered
        selected = _selection_candidates(candidates, checks, required, selectable_hotel_ids)
        self.assertEqual([c.id for c in selected], ["win_unk"])

    def test_selection_candidates_excludes_sold_out_even_if_within_standard(self):
        """Even if a sold_out hotel is within_standard, it must NOT enter the selection pool."""
        h_over_avail = make_hotel("over_avail", 800, availability="available")
        h_win_sold_out = make_hotel("win_sold", 300, availability="sold_out")
        h_win_unknown = make_hotel("win_unk", 350, availability="unknown")

        candidates = [h_over_avail, h_win_sold_out, h_win_unknown]
        checks = {
            "over_avail": {"status": "over_standard", "cap": "400", "amount": "800", "currency": "CNY", "excess": "400"},
            "win_sold": {"status": "within_standard", "cap": "400", "amount": "300", "currency": "CNY", "excess": "0"},
            "win_unk": {"status": "within_standard", "cap": "400", "amount": "350", "currency": "CNY", "excess": "0"},
        }
        required = {"hotel"}
        selectable_hotel_ids = {"over_avail"}

        # win_sold is sold_out, so it must be excluded. Only win_unk should be in the selection pool!
        selected = _selection_candidates(candidates, checks, required, selectable_hotel_ids)
        self.assertEqual([c.id for c in selected], ["win_unk"])


if __name__ == "__main__":
    unittest.main()
