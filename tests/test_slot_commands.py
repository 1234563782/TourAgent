"""实验协议机械边界；不把模型归类当成语义证明。"""

from copy import deepcopy
import unittest

from scripts.slot_command_experiment import CommandSubmission, apply_commands


def operation(field, value, *, action="set", scope="asserted", quote="原文", index=0):
    return {"field": field, "value": value, "action": action, "scope": scope,
            "evidence": {"user_message": index, "quote": quote}}


def submission(operations, **kwargs):
    return CommandSubmission.model_validate({"tasks": [{
        "intent": "policy_check", "question": "核对", "operations": operations, **kwargs,
    }]})


class CommandTests(unittest.TestCase):
    def test_hypothesis_does_not_become_actual_fact(self):
        result = apply_commands(submission([
            operation("kind", "hotel"), operation("amount", "800", scope="hypothetical"),
        ]), {}, ["原文"], 4)
        self.assertNotIn("amount", result["tasks"][0]["subject"]["values"])
        self.assertEqual(result["tasks"][0]["scenario"]["values"]["amount"], "800")
        self.assertTrue(result["experimental_unreviewed"])

    def test_actual_and_hypothetical_value_can_coexist(self):
        result = apply_commands(submission([
            operation("amount", "350"), operation("amount", "450", scope="hypothetical"),
        ]), {}, ["原文"], 4)
        self.assertEqual(result["tasks"][0]["subject"]["values"]["amount"], "350")
        self.assertEqual(result["tasks"][0]["scenario"]["values"]["amount"], "450")

    def test_increment_changes_only_submitted_field(self):
        initial = apply_commands(submission([
            operation("kind", "hotel"), operation("amount", "476"),
            operation("destination", "南昌"),
        ]), {}, ["原文"], 4)["slots"]
        before = deepcopy(initial)
        subject = initial["expense"]
        result = apply_commands(submission([
            operation("amount", "467", quote="改金额", index=1),
        ], action="update", target_id=subject["id"], base_revision=subject["revision"]),
            initial, ["原文", "改金额"], 4)
        self.assertEqual(initial, before)
        self.assertEqual(result["slots"]["expense"]["id"], subject["id"])
        self.assertEqual(result["slots"]["expense"]["values"]["destination"], "南昌")
        self.assertEqual(result["slots"]["expense"]["values"]["amount"], "467")
        self.assertEqual(result["slots"]["expense"]["revision"], subject["revision"] + 1)

    def test_clear_is_null_not_false(self):
        result = apply_commands(submission([
            operation("special_approval", None, action="clear"),
        ]), {}, ["原文"], 4)
        values = result["tasks"][0]["subject"]["values"]
        self.assertIn("special_approval", values)
        self.assertIsNone(values["special_approval"])

    def test_clear_non_null_rejected(self):
        with self.assertRaises(ValueError):
            submission([operation("special_approval", False, action="clear")])

    def test_set_null_rejected(self):
        with self.assertRaises(ValueError):
            submission([operation("special_approval", None)])

    def test_invalid_field_or_business_value_rejected(self):
        with self.assertRaises(ValueError):
            submission([operation("provenance", "trusted")])
        with self.assertRaises(ValueError):
            apply_commands(submission([operation("amount", "-1")]), {}, ["原文"], 4)

    def test_hypothesis_requires_real_current_evidence(self):
        with self.assertRaises(ValueError):
            apply_commands(submission([
                operation("amount", "800", scope="hypothetical", quote="不存在"),
            ]), {}, ["原文"], 4)

    def test_conflicting_duplicate_operation_rejected(self):
        with self.assertRaises(ValueError):
            apply_commands(submission([
                operation("amount", "350"), operation("amount", "450"),
            ]), {}, ["原文"], 4)

    def test_late_failure_does_not_commit_earlier_task(self):
        good = submission([operation("amount", "350")]).model_dump()
        bad = submission([operation("amount", "450", quote="不存在")]).model_dump()
        commands = CommandSubmission.model_validate({"tasks": good["tasks"] + bad["tasks"]})
        current = {}
        with self.assertRaises(ValueError):
            apply_commands(commands, current, ["原文"], 4)
        self.assertEqual(current, {})

    def test_other_cannot_write_business_state(self):
        commands = CommandSubmission.model_validate({"tasks": [{
            "intent": "other", "question": "天气", "operations": [operation("amount", "1")],
        }]})
        with self.assertRaises(ValueError):
            apply_commands(commands, {}, ["原文"], 4)

    def test_stale_subject_rejected(self):
        with self.assertRaises(ValueError):
            apply_commands(submission([operation("amount", "1")],
                                      action="update", target_id="missing", base_revision=1),
                           {}, ["原文"], 4)


if __name__ == "__main__":
    unittest.main()
