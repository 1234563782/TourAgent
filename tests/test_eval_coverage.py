"""Coverage extraction is static and must not imply current model success."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from evals.build_coverage import build, duplicate_groups, extract_group, recovery_observation, write_outputs
from evals.extract_candidates import read_json, validate_references


ROOT = Path(__file__).resolve().parents[1]


class CoverageExtractionTests(unittest.TestCase):
    def test_ast_does_not_execute_module_and_keeps_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.py").write_text(
                "raise RuntimeError('must not execute')\n"
                "class Example:\n"
                "    def test_guard(self):\n"
                "        self.assertEqual(1, 1)\n"
                "        assert True\n", encoding="utf-8")
            rows = extract_group(root, {"path": "sample.py", "environment": "offline",
                                        "category": "guard", "methods": ["test_guard"]})
            self.assertEqual(rows[0]["selector"], "sample.Example.test_guard")
            self.assertEqual(len(rows[0]["assertion_refs"]), 2)
            self.assertEqual(rows[0]["execution_status"], "not_run")
            self.assertGreater(validate_references(root, rows), 0)
            (root / "sample.py").write_text("# changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source changed"):
                validate_references(root, rows)

    def test_missing_method_and_wrong_live_environment_fail(self):
        group = read_json(ROOT / "evals/coverage_sources.json")["groups"][1]
        with self.assertRaisesRegex(ValueError, "live tests"):
            extract_group(ROOT, {**group, "environment": "offline"})
        with self.assertRaisesRegex(ValueError, "expected one method"):
            extract_group(ROOT, {**group, "methods": ["test_does_not_exist"]})

    def test_batch_failure_is_not_assigned_to_individual_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "summary.json"
            path.write_text(json.dumps({"passed": False, "checks": ["a", "b"],
                                        "realModel": False}), encoding="utf-8")
            row = recovery_observation(root, path)
            self.assertEqual(row["reported_batch_outcome"], "failed")
            self.assertFalse(row["real_model"])
            self.assertEqual({c["individual_outcome"] for c in row["checks"]}, {"unknown"})
            path.write_text('{"passed": [], "checks": []}', encoding="utf-8")
            with self.assertRaises(ValueError):
                recovery_observation(root, path)

    def test_duplicate_groups_do_not_merge_or_match_last_turn_only(self):
        candidates = [
            {"candidate_id": "a", "conversation": ["one", "last"], "required_context": 1},
            {"candidate_id": "b", "conversation": ["one", "last"], "required_context": 2},
            {"candidate_id": "c", "conversation": ["two", "last"], "required_context": 1},
        ]
        before = copy.deepcopy(candidates)
        groups = duplicate_groups(candidates)
        self.assertEqual(candidates, before)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["candidate_ids"], ["a", "b"])

    def test_real_manifest_is_reproducible_pending_and_source_validated(self):
        manifest = read_json(ROOT / "evals/coverage_sources.json")
        candidates = [json.loads(line) for line in
                      (ROOT / "evals/candidates/candidates.jsonl").read_text(encoding="utf-8").splitlines()]
        result = build(ROOT, manifest, candidates)
        self.assertEqual(result, build(ROOT, manifest, candidates))
        self.assertEqual(result["summary"]["reviewed_gold"], 0)
        self.assertTrue(result["engineering_scenarios"])
        for row in result["engineering_scenarios"]:
            self.assertEqual(row["execution_status"], "not_run")
            self.assertFalse(row["real_model_quality_evidence"])
            self.assertTrue(row["assertion_refs"])
        for row in result["proposed_cases"]:
            self.assertEqual(row["kind"], "synthetic_proposal")
            self.assertEqual(row["review_status"], "pending")
            self.assertIsNone(row["reviewed_expected"])
            self.assertFalse(row["replay_ready"])
        self.assertGreater(validate_references(ROOT, result), 0)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_outputs(output, result)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            write_outputs(output, build(ROOT, manifest, candidates))
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_duplicate_selectors_rejected(self):
        manifest = read_json(ROOT / "evals/coverage_sources.json")
        manifest["groups"].append(manifest["groups"][0])
        with self.assertRaisesRegex(ValueError, "duplicate engineering selector"):
            build(ROOT, manifest, [])


if __name__ == "__main__":
    unittest.main()
