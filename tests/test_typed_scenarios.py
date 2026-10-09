"""强类型假设候选的程序边界，不代表真实模型语义验收。"""

from copy import deepcopy
import unittest
import json
from pathlib import Path

from scripts.typed_scenario_experiment import TypedSubmission, apply_typed
from scripts.compare_slot_strategies import validate_case_fields
from scripts.rescore_slot_results import rescore
from business_travel.slots import SlotTask, stage_submission


def task(expense=None, scenario=None, index=0, quote="原文", **kwargs):
    def evidence(values):
        return {key: {"user_message": index, "quote": quote}
                for key in (values or {}) if key != "kind"}
    return {"intent": "policy_check", "question": "核对",
            "expense": expense if expense is not None else {"kind": "hotel"},
            "evidence": evidence(expense), "scenario": scenario,
            "scenario_evidence": evidence(scenario), **kwargs}


def apply(tasks, current=None, users=None):
    return apply_typed(TypedSubmission(tasks=tasks), current or {}, users or ["原文"], 8)


class TypedScenarioTests(unittest.TestCase):
    def test_identical_fields_without_new_evidence_are_noops(self):
        state = apply([task({"kind": "hotel", "amount": "381", "destination": "杭州"})])["slots"]
        before = deepcopy(state)
        subject = state["expense"]
        proposal = SlotTask(intent="policy_check", action="update", target_id=subject["id"],
            base_revision=subject["revision"], expense={"destination": "杭州", "amount": "367"},
            evidence={"amount": {"user_message": 1, "quote": "367"}})
        updated = stage_submission(proposal, state, ["原文", "367"])
        self.assertEqual(state, before)
        self.assertEqual(updated["expense"]["values"]["amount"], "367")
        self.assertEqual(updated["expense"]["evidence"]["destination"], subject["evidence"]["destination"])
        self.assertEqual(len(updated["expense"]["history"]), len(subject["history"]) + 1)

    def test_new_value_without_evidence_is_still_rejected(self):
        state = apply([task({"kind": "hotel", "destination": "杭州"})])["slots"]
        subject = state["expense"]
        with self.assertRaises(ValueError):
            stage_submission(SlotTask(intent="policy_check", action="update",
                target_id=subject["id"], base_revision=subject["revision"],
                expense={"destination": "宁波"}), state, ["原文", "更新"])

    def test_noop_does_not_revoke_confirmation_or_change_revision(self):
        state = apply([task({"kind": "hotel", "destination": "杭州"})])["slots"]
        subject = state["expense"]
        subject["fact_confirmation"] = {"accepted": True}
        updated = stage_submission(SlotTask(intent="policy_check", action="update",
            target_id=subject["id"], base_revision=subject["revision"],
            expense={"kind": "hotel", "destination": "杭州"}), state, ["原文", "重查"])
        self.assertEqual(updated["expense"]["revision"], subject["revision"])
        self.assertEqual(updated["expense"]["fact_confirmation"], subject["fact_confirmation"])

    def test_acceptance_contract_is_valid(self):
        cases = json.loads((Path(__file__).parent / "typed_scenarios_acceptance.json").read_text(
            encoding="utf-8"))
        validate_case_fields(cases)

    def test_invalid_expected_field_fails_before_model_calls(self):
        with self.assertRaises(ValueError):
            validate_case_fields([{"id": "bad", "turns": [{"expected": [{
                "intent": "policy_check", "values": {}, "scenario": {"tax_included": True}}]}]}])

    def test_rescore_cannot_change_user_input(self):
        with self.assertRaisesRegex(ValueError, "input_changed"):
            rescore({"records": [{"id": "one", "turn": 1, "question": "原文"}]},
                [{"id": "one", "turns": [{"question": "改文", "expected": []}]}])

    def test_hypothetical_price_never_enters_actual_state(self):
        result = apply([task(scenario={"amount": "813"})])
        self.assertNotIn("amount", result["slots"]["expense"]["values"])
        self.assertEqual(result["tasks"][0]["scenario"]["values"]["amount"], "813")
        self.assertEqual(result["tasks"][0]["scenario"]["values"]["kind"], "hotel")
        self.assertTrue(result["experimental_unreviewed"])

    def test_actual_and_scenario_merge_without_confirmation_inheritance(self):
        state = apply([task({"kind": "hotel", "amount": "381", "destination": "杭州"})])["slots"]
        state["expense"]["fact_confirmation"] = {"accepted": True}
        before = deepcopy(state)
        subject = state["expense"]
        result = apply([task({}, {"amount": "492"}, index=1, quote="假设",
            action="update", target_id=subject["id"], base_revision=subject["revision"])],
            state, ["原文", "假设"])
        self.assertEqual(state, before)
        self.assertEqual(result["slots"]["expense"]["values"]["amount"], "381")
        scenario = result["tasks"][0]["scenario"]
        self.assertEqual(scenario["values"]["destination"], "杭州")
        self.assertEqual(scenario["values"]["amount"], "492")
        self.assertNotIn("fact_confirmation", scenario)
        self.assertEqual(scenario["based_on"]["id"], subject["id"])

    def test_next_turn_correction_inherits_actual_not_scenario(self):
        state = apply([task({"kind": "hotel", "amount": "381", "destination": "杭州"},
                            {"amount": "492", "destination": "宁波"})])["slots"]
        subject = state["expense"]
        result = apply([task({"amount": "367", "special_approval": None},
            index=1, quote="更正", action="update", target_id=subject["id"],
            base_revision=subject["revision"])], state, ["原文", "更正"])
        self.assertEqual(result["slots"]["expense"]["values"], {
            "kind": "hotel", "amount": "367", "destination": "杭州", "special_approval": None})
        self.assertNotIn("scenario", result["tasks"][0])
        self.assertEqual(result["slots"]["expense"]["id"], subject["id"])

    def test_scenario_error_rejects_whole_batch(self):
        state = apply([task({"kind": "hotel", "amount": "381"})])["slots"]
        before = deepcopy(state)
        with self.assertRaises(ValueError):
            apply([task({"amount": "24"}), task(scenario={"amount": "492"}, quote="不存在")], state)
        self.assertEqual(state, before)

    def test_scenario_rejects_stale_reference(self):
        with self.assertRaises(ValueError):
            apply([task({}, {"amount": "492"}, action="update",
                        target_id="missing", base_revision=1)])

    def test_scenario_rejects_old_evidence(self):
        with self.assertRaises(ValueError):
            apply([task(scenario={"amount": "492"})], users=["原文", "新消息"])

    def test_scenario_cannot_be_attached_to_non_policy_intent(self):
        for intent in ("plan_trip", "other"):
            with self.subTest(intent=intent), self.assertRaises(ValueError):
                apply([task(scenario={"amount": "492"}, intent=intent)])

    def test_missing_scenario_rejects_orphan_evidence(self):
        value = task()
        value["scenario_evidence"] = {"amount": {"user_message": 0, "quote": "原文"}}
        with self.assertRaises(ValueError):
            apply([value])

    def test_invalid_type_and_forged_metadata_rejected(self):
        for scenario in ({"amount": -1}, {"fact_confirmation": True}):
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                apply([task(scenario=scenario)])


if __name__ == "__main__":
    unittest.main()
