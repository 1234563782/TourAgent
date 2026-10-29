"""Real institutional package, deterministic calculations, synthetic database/model boundaries."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from langchain_core.messages import HumanMessage, ToolMessage
from pydantic import ValidationError

from business_travel.graph import build_assistant
from business_travel.memory import TravelMemory
from business_travel.policy_guard import render_policy
from business_travel.policy_lodging import LodgingRow
from business_travel.policy_package import prepare_package
from business_travel.policy_pg import PostgresPolicyStore
from business_travel.policy_rules import ExpenseFacts, ReviewedRules, check_expense
from business_travel.reimbursement import bind_estimate_sources, estimate_expense
from business_travel.slot_protocol import ExpensePatch
from business_travel.slots import SlotTask, SlotError, stage_submission
from business_travel.university_policy import UniversityRules, check_university_expense
from test_policy_rag import database_doc, settings, store_with_db
from test_travel_graph import ScriptedModel, slot_response, review_response


ROOT = Path(__file__).resolve().parents[1]


class ReimbursementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = prepare_package(ROOT / "policies/hfut-2026/manifest.json")
        cls.rules = UniversityRules.model_validate(cls.bundle.rules)
        cls.rows = [LodgingRow.model_validate(row) for row in cls.bundle.lodging_rows]
        cls.doc = database_doc(id=7, version=cls.bundle.version, status="published",
                               source_sha256=cls.bundle.source_sha256,
                               effective_from=cls.bundle.effective_from, effective_to=cls.bundle.effective_to)
        cls.chunks = [dict(chunk_id=c.id, content=c.text, clause=c.clause,
                           section=c.section, table_data=c.table) for c in cls.bundle.chunks]
        cls.sources = [PostgresPolicyStore._source(row, cls.doc) for row in cls.chunks]

    def facts(self, **changes):
        return ExpenseFacts.model_validate({
            "kind": "hotel", "expense_date": "2026-09-16", "destination": "合肥",
            "destination_province": "安徽", "mainland_business_trip": True,
            "personnel_type": "staff", "lodging_standard": "其他人员",
            "amount": "350", "currency": "CNY", "basis": "room_night", "occupants": 1,
            "taxes_confirmed": True, "date_verified": True, **changes})

    def calculate(self, facts=None, **changes):
        return estimate_expense(
            facts or self.facts(**changes),
            lambda unit: check_university_expense(unit, self.rules, self.rows),
            max_nights=366, currency=self.rules.currency,
            effective_from=self.bundle.effective_from, effective_to=self.bundle.effective_to)

    def bound(self, **changes):
        result = self.calculate(**changes)
        bind_estimate_sources(result, self.sources, self.doc)
        return result

    def test_standard_amount_has_same_version_real_body_and_table_evidence(self):
        result = self.bound()
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal("350"))
        self.assertFalse(result["approved"])
        item = result["estimate"]["items"][0]
        self.assertTrue(item["evidence_verified"])
        self.assertTrue(set(item["row_ids"]) <= set(item["evidence_ids"]))
        self.assertTrue(any(source["source"].endswith(".xls") for source in result["sources"]))

    def test_multi_night_uniform_amount_is_multiplied_only_when_explicit(self):
        estimate = self.bound(check_out="2026-09-18", amount_scope="uniform_nightly")["estimate"]
        self.assertEqual(Decimal(estimate["reference_amount"]), Decimal("700"))
        self.assertEqual(Decimal(estimate["cap_total"]), Decimal("700"))
        self.assertEqual([item["date"] for item in estimate["items"]], ["2026-09-16", "2026-09-17"])

    def test_total_is_never_averaged_even_below_combined_limit(self):
        result = self.bound(amount="600", amount_scope="stay_total", check_out="2026-09-18")
        self.assertIsNone(result["estimate"]["reference_amount"])
        self.assertEqual(result["missing_fields"], ["nightly_amounts"])
        self.assertTrue(all(item["amount"] is None for item in result["estimate"]["items"]))

    def test_unequal_nights_cannot_offset_an_over_limit_night(self):
        result = self.bound(amount="700", amount_scope="stay_total", check_out="2026-09-18",
                            nightly_amounts=[{"date": "2026-09-16", "amount": "400"},
                                             {"date": "2026-09-17", "amount": "300"}])
        estimate = result["estimate"]
        self.assertEqual(result["status"], "over_standard")
        self.assertIsNone(estimate["reference_amount"])
        self.assertEqual(Decimal(estimate["known_reference_amount"]), Decimal("300"))
        self.assertEqual(Decimal(estimate["excess"]), Decimal("50"))
        self.assertEqual(Decimal(estimate["standard_portion"]), Decimal("650"))

    def test_season_boundary_is_evaluated_per_night(self):
        estimate = self.bound(destination="青岛", destination_province="山东", expense_date="2026-06-30",
                              check_out="2026-07-02", amount="400", amount_scope="uniform_nightly")["estimate"]
        self.assertEqual([Decimal(item["cap"]) for item in estimate["items"]], [Decimal(380), Decimal(450)])
        self.assertEqual(Decimal(estimate["excess"]), Decimal(20))
        self.assertIsNone(estimate["reference_amount"])

    def test_partial_dates_are_unknown_not_zero(self):
        result = self.bound(amount=None, check_out="2026-09-18",
                            nightly_amounts=[{"date": "2026-09-16", "amount": "300.19"}])
        self.assertIsNone(result["estimate"]["reference_amount"])
        self.assertEqual(result["estimate"]["known_reference_amount"], "300.19")
        self.assertIsNone(result["estimate"]["submitted_amount"])
        self.assertIn("nightly_amounts", result["missing_fields"])

    def test_dates_duplicates_and_conflicting_totals_fail_without_an_amount_conclusion(self):
        for changes in (
            {"check_out": "2026-09-16"},
            {"check_out": "2028-01-01"},
            {"amount_scope": "uniform_nightly"},
            {"amount": None, "nightly_amounts": [{"date": "2026-09-16", "amount": "1"}] * 2},
            {"amount_scope": "stay_total", "nightly_amounts": [{"date": "2026-09-16", "amount": "1"}]},
            {"amount": None, "nightly_amounts": [{"date": "2026-09-18", "amount": "1"}]},
        ):
            with self.subTest(changes=changes):
                result = self.bound(**changes)
                self.assertEqual(result["status"], "insufficient_information")
                self.assertIsNone(result["estimate"]["reference_amount"])

    def test_money_is_exact_and_invalid_values_are_rejected(self):
        estimate = self.bound(amount="0.10", amount_scope="uniform_nightly", check_out="2026-09-19")["estimate"]
        self.assertEqual(Decimal(estimate["reference_amount"]), Decimal("0.30"))
        for value in ("NaN", "Infinity", "-0.01"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.facts(amount=value)

    def test_hotel_over_limit_is_not_automatically_capped_or_zero(self):
        result = self.bound(amount="500")
        self.assertEqual(result["status"], "over_standard")
        self.assertIsNone(result["estimate"]["reference_amount"])
        self.assertEqual(Decimal(result["estimate"]["excess"]), Decimal(150))

    def test_approval_sharing_student_and_currency_do_not_automatically_pass(self):
        for changes in ({"special_approval": True}, {"occupants": 2},
                        {"personnel_type": "student"}, {"mainland_business_trip": False},
                        {"organizer_arranged_lodging": True}, {"currency": "USD"},
                        {"date_verified": False}, {"taxes_confirmed": False}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.bound(**changes)["estimate"]["reference_amount"])

    def test_incompatible_currency_or_shared_room_never_has_a_fake_difference(self):
        for changes in ({"currency": "USD", "amount": "500"}, {"occupants": 2, "amount": "500"},
                        {"taxes_confirmed": False, "amount": "500"}):
            with self.subTest(changes=changes):
                estimate = self.bound(**changes)["estimate"]
                self.assertIsNone(estimate["standard_portion"])
                self.assertIsNone(estimate["excess"])
                self.assertEqual(estimate["policy_currency"], "CNY")

    def test_hypothetical_amount_keeps_its_scenario(self):
        result = self.bound(expense_scenario="hypothetical")
        self.assertEqual(result["estimate"]["scenario"], "hypothetical")
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal(350))

    def test_whole_stay_must_be_in_published_period(self):
        result = self.bound(expense_date="2026-12-31", check_out="2027-01-02", amount_scope="uniform_nightly")
        self.assertEqual(result["reason"], "expense_outside_published_period")
        self.assertIsNone(result["estimate"]["reference_amount"])

    def ticket(self, **changes):
        return self.bound(**{"kind": "transport", "transport_standard": "三类", "transport_mode": "high_speed_train",
                             "seat_class": "二等座", "basis": "ticket", "transport_amount_scope": "fare_only", **changes})

    def test_train_and_flight_fare(self):
        for mode, seat in (("high_speed_train", "二等座"), ("flight", "经济舱")):
            result = self.bound(kind="transport", transport_standard="三类", transport_mode=mode,
                                seat_class=seat, basis="ticket", transport_amount_scope="fare_only", amount="480.20")
            self.assertEqual(result["estimate"]["reference_amount"], "480.20")
            self.assertIsNone(result["estimate"]["excess"])

    def test_transport_citation_requires_its_category_table_not_just_a_clause_heading(self):
        result = self.calculate(self.facts(kind="transport", transport_standard="三类", transport_mode="flight",
                                          seat_class="经济舱", basis="ticket", transport_amount_scope="fare_only"))
        sources = [source for source in self.sources if not (
            (source.get("table") or {}).get("cells")
            and "三类" in source["table"]["cells"])]
        bind_estimate_sources(result, sources, self.doc)
        self.assertIsNone(result["estimate"]["reference_amount"])
        self.assertEqual(result["reason"], "estimate_evidence_incomplete")

    def test_transport_does_not_display_other_categories_as_its_evidence(self):
        result = self.ticket()
        item = result["estimate"]["items"][0]
        used = [source for source in result["sources"] if source["chunk"] in item["evidence_ids"]]
        categories = [(source.get("table") or {}).get("cells", []) for source in used]
        self.assertTrue(any("三类" in cells for cells in categories))
        self.assertFalse(any("一类" in cells or "二类" in cells for cells in categories))

    def test_mixed_airline_total_only_estimates_explicit_fare(self):
        result = self.bound(kind="transport", transport_standard="三类", transport_mode="flight",
                            seat_class="经济舱", basis="ticket", transport_amount_scope="total",
                            amount="900", fare_amount="800")
        self.assertIsNone(result["estimate"]["reference_amount"])
        self.assertEqual(result["estimate"]["known_reference_amount"], "800")
        self.assertEqual(result["estimate"]["unassessed_amount"], "100")

    def test_over_standard_seat_has_no_invented_economy_price(self):
        result = self.ticket(seat_class="商务座")
        self.assertEqual(result["status"], "over_standard")
        self.assertIsNone(result["estimate"]["reference_amount"])
        self.assertIsNone(result["estimate"]["excess"])

    def test_unknown_ticket_breakdown_and_false_confirmations_block_amount(self):
        for changes in ({"transport_amount_scope": "unknown"}, {"currency": "USD"},
                        {"taxes_confirmed": False}, {"date_verified": False}, {"amount": None}):
            with self.subTest(changes=changes):
                facts = self.facts(kind="transport", transport_standard="三类", transport_mode="flight",
                                   seat_class="经济舱", basis="ticket")
                facts = facts.model_copy(update={"transport_amount_scope": "fare_only"})
                facts = ExpenseFacts.model_validate({**facts.model_dump(), **changes})
                result = self.calculate(facts)
                self.assertIsNone(result["estimate"]["reference_amount"])

    def test_missing_table_body_or_wrong_version_removes_monetary_conclusions(self):
        for action in ("table", "body", "version", "config"):
            result = self.calculate()
            sources = copy.deepcopy(self.sources)
            if action == "table":
                sources = [source for source in sources if source["chunk"] not in result["row_ids"]]
            elif action == "body":
                sources = [source for source in sources if source["clause"] not in result["source_clauses"]]
            elif action == "version":
                for source in sources:
                    source["version"] = "another-version"
            else:
                for source in sources:
                    source["source_type"] = "reviewed_rule"
            with self.subTest(action=action):
                bind_estimate_sources(result, sources, self.doc)
                self.assertEqual(result["reason"], "estimate_evidence_incomplete")
                self.assertIsNone(result["estimate"]["reference_amount"])
                self.assertIsNone(result["estimate"]["cap_total"])

    def database(self, facts, *, mode="estimate"):
        computed = self.calculate(facts)
        body = [row for row in self.chunks if row["clause"] in computed["source_clauses"]]
        table = [row for row in self.chunks if row["chunk_id"] in computed["row_ids"]]
        replies = [[self.doc], {"rules": self.bundle.rules}, [{"data": row} for row in self.bundle.lodging_rows], body]
        if table:
            replies.append(table)
        replies.append(None)
        store, connection, _ = store_with_db(replies)
        return (store.estimate(facts) if mode == "estimate" else store.check(facts)), connection

    def test_postgres_uses_one_version_one_rule_read_and_safe_audit(self):
        result, conn = self.database(self.facts(check_out="2026-09-18", amount_scope="uniform_nightly"))
        self.assertEqual(Decimal(result["estimate"]["reference_amount"]), Decimal(700))
        self.assertEqual(sum("SELECT rules FROM" in call.args[0] for call in conn.execute.call_args_list), 1)
        audit = next(call for call in conn.execute.call_args_list if "INSERT INTO policy_check_logs" in call.args[0])
        self.assertEqual(set(audit.args[1][-1].obj), {"status", "reason", "source_clauses"})

    def test_legacy_check_remains_single_night_without_estimate(self):
        result, _ = self.database(self.facts(), mode="legacy")
        self.assertEqual(result["status"], "within_checked_rules")
        self.assertNotIn("estimate", result)

    def test_company_policy_retains_its_room_night_unit(self):
        rules = ReviewedRules.model_validate_json((ROOT / "policies/demo-v1.1.rules.json").read_text("utf-8"))
        facts = ExpenseFacts.model_validate_json((ROOT / "policies/demo-hotel-facts.json").read_text("utf-8"))
        result = estimate_expense(facts, lambda unit: check_expense(unit, rules),
                                 max_nights=366, currency=rules.currency)
        self.assertEqual(result["estimate"]["items"][0]["limit_unit"], "room_night")

    def test_display_places_missing_conditions_last_and_deduplicates_sources(self):
        result = self.bound(check_out="2026-09-18", amount_scope="uniform_nightly")
        data = {"check_mode": "estimate", "rule_checks": [{"facts": self.facts().model_dump(mode="json"),
                                                           "result": result}]}
        rendered = render_policy(data)
        self.assertLess(rendered.index("可报参考金额"), rendered.index("补充说明"))
        self.assertIn("[来源1]", rendered)
        self.assertEqual(rendered.count(":::details 政策依据 · 来源1\n"), 1)
        self.assertNotIn("项目已审核规则配置", rendered)
        self.assertIn("原文摘录", rendered)

    def test_hypothetical_update_cannot_overwrite_actual_subject(self):
        old = {"id": "expense-1", "revision": 1, "domain": "expense", "values": {"amount": "350"},
               "evidence": {}, "history": []}
        before = copy.deepcopy(old)
        task = SlotTask(intent="policy_check", action="update", target_id=old["id"], base_revision=1,
                        expense=ExpensePatch(expense_scenario="hypothetical", amount="400"))
        with self.assertRaisesRegex(SlotError, "expense_scenario_change_requires_new_task"):
            stage_submission(task, {"expense": old}, ["假如花400元呢"])
        self.assertEqual(old, before)

    def test_graph_selects_estimate_or_legacy_without_extra_model_calls(self):
        test = self

        class Policy:
            def __init__(self):
                self.calls = []

            def estimate(self, facts):
                self.calls.append("estimate")
                return test.bound(**facts.model_dump(exclude_unset=True))

            def check(self, facts):
                self.calls.append("check")
                return check_university_expense(facts, test.rules, test.rows)

        facts = self.facts().model_dump(mode="json", exclude_unset=True)
        text = "请测算住宿费：" + json.dumps(facts, ensure_ascii=False)
        for mode in ("estimate", "legacy"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = replace(settings(), storage_dir=root, legacy_memory_dir=root / "legacy", policy_check_mode=mode)
                memory = TravelMemory(root, root / "legacy", "test")
                policy = Policy()
                models = {"slots": ScriptedModel([slot_response(facts, "policy_check", {key: text for key in facts})]),
                          "reviewer": ScriptedModel([review_response()]), "supervisor": ScriptedModel([])}
                graph = build_assistant(lambda role: models[role], config, memory, policy, provider=Mock())
                result = graph.invoke({"messages": [HumanMessage(content=text)]},
                                      {"configurable": {"thread_id": mode}, "recursion_limit": 30})
                self.assertEqual(policy.calls, ["estimate" if mode == "estimate" else "check"])
                tool = next(message for message in result["messages"] if isinstance(message, ToolMessage))
                self.assertEqual("费用报销测算" in json.loads(tool.content)["answer"], mode == "estimate")
