"""Bind manually written annotation drafts to existing candidates and evidence."""

import argparse
import json
from pathlib import Path

from evals.build_coverage import extract_group
from evals.extract_candidates import read_json, source_ref, validate_references
from evals.review_candidates import make_queue, read_rows, template, validate_decision, write_rows


def build_drafts(root, specs, candidates, proposals):
    queue = {row["candidate_id"]: row for row in make_queue(candidates, proposals)}
    fixture_path = "tests/preference_dialogue_cases.json"
    drafts = []
    seen = set()
    for index, spec in enumerate(specs["cases"]):
        if "fixture_id" in spec:
            matches = [
                item for item in candidates if any(
                    e.get("kind") == "test_fixture"
                    and e.get("original_case_id") == spec["fixture_id"]
                    and e["source"]["path"] == fixture_path
                    for e in item.get("evidence", [])
                )
            ]
            if len(matches) != 1:
                raise ValueError(f"expected one fixture candidate: {spec['fixture_id']}")
            key = matches[0]["candidate_id"]
        else:
            key = spec["candidate_id"]
        if key in seen:
            raise ValueError("duplicate annotation candidate")
        seen.add(key)
        row = queue[key]
        turns = [i for i, msg in enumerate(row["conversation"], 1) if msg["role"] == "user"]
        if len(turns) != len(spec["turns"]):
            raise ValueError(f"turn count mismatch: {key}")
        basis = []
        for path, method in spec["basis"]:
            basis.extend(extract_group(root, {
                "path": path, "methods": [method], "environment": "offline",
                "category": spec["category"],
            }))
        policy = {
            "applicability": "not_applicable", "sources": [],
            "reason": "Memory/state contract or synthetic review fixture; no real policy amount asserted.",
        }
        if spec.get("policy_required"):
            manifest_path = root / "policies/hfut-2026/manifest.json"
            manifest = read_json(manifest_path)
            policy = {
                "applicability": "required",
                "version": manifest["version"],
                "scope": "project-adopted package; not externally verified current policy",
                "sources": [source_ref(root, manifest_path)] + [
                    source_ref(root, manifest_path.parent / p)
                    for p in [manifest["snapshot"], manifest["rules"],
                              *(s["path"] for s in manifest["sources"])]
                ],
            }
        decision = {
            **template(row),
            "rationale": "Agent draft checked against cited fixture and test contracts; needs human review.",
            "category": spec["category"],
            "initial_state": {
                **specs["shared_initial_state"],
                "conversation_state": spec["conversation_state"],
                "memory_state": spec["memory_state"],
                "fault_injection": spec.get("fault_injection", specs["shared_initial_state"]["fault_injection"]),
            },
            "policy": policy,
            "turn_assertions": [
                {"turn": turn, **assertion} for turn, assertion in zip(turns, spec["turns"], strict=True)
            ],
            "resolved_migration_flags": [],
        }
        # Validate completeness without saving an approval or claiming human review.
        validate_decision(root, row, {
            **decision, "decision": "approve", "reviewer": "agent_structural_validation_only",
            "resolved_migration_flags": row["migration_flags"],
        })
        drafts.append({
            "candidate_id": key,
            "author_type": "agent",
            "status": "draft_requires_human_review",
            "conversation": row["conversation"],
            "annotation": decision,
            "candidate_sources": row["sources"],
            "annotation_source": source_ref(root, root / "evals/annotation_specs.json", f"/cases/{index}"),
            "engineering_basis": [
                {"selector": r["selector"], "source": r["source"],
                 "execution_status": "not_run"} for r in basis
            ],
            "migration_assessment": {
                "flags": row["migration_flags"],
                "note": "Draft uses current autosave contract; reviewer must confirm migration before approval.",
            },
            "open_questions": spec["open_questions"],
            "real_model_execution": "not_run",
            "replay_ready": False,
        })
    validate_references(root, drafts)
    return drafts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    drafts = build_drafts(
        root, read_json(root / "evals/annotation_specs.json"),
        read_rows(root / "evals/candidates/candidates.jsonl"),
        read_rows(root / "evals/coverage/proposed_cases.jsonl"))
    output = root / "evals/review"
    output.mkdir(parents=True, exist_ok=True)
    write_rows(output / "annotation_drafts.jsonl", drafts)
    lines = ["# 首批内容标注草稿", "",
             "15条来源绑定标注；由Agent拟定，未替代人工审核，未执行真实模型。",
             "工程测试仅支持所述边界，不代表原始自然语言已通过。",
             "decisions.jsonl保持不变；本文件不会自动晋升gold。", ""]
    for draft in drafts:
        annotation = draft["annotation"]
        lines += [f"## {draft['candidate_id']}", "", "### 原始输入", ""]
        for i, message in enumerate(draft["conversation"], 1):
            lines.append(f"{i}. {message['content']}")
        lines += ["", "### 初态", ""]
        lines += [f"- {key}: {value}" for key, value in annotation["initial_state"].items()]
        lines += ["", "### 逐轮断言", ""]
        for turn in annotation["turn_assertions"]:
            lines += [f"- 第{turn['turn']}轮必须：{'；'.join(turn['must'])}",
                      f"- 第{turn['turn']}轮禁止：{'；'.join(turn['must_not'])}",
                      f"- 观察：{turn['observable']}"]
        lines += ["", "### 证据及边界", ""]
        lines += [f"- `{item['selector']}`" for item in draft["engineering_basis"]]
        lines += [f"- 待确认：{question}" for question in draft["open_questions"]]
        lines += ["- 状态：待人工审核；真实模型未运行。", ""]
    (output / "annotation_drafts.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"drafts": len(drafts), "real_model_runs": 0,
                      "human_approvals": 0,
                      "validated_source_locators": validate_references(root, drafts)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
