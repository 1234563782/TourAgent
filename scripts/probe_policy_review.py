"""仅对指定合成验收证据做真实模型复核对比，不写业务数据库。"""

import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from business_travel.context_budget import budgeted_model
from business_travel.models import create_model
from business_travel.policy_rag import PolicyAnswerReview, _review_prompt, _submit
from business_travel.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--question-without-rewrite", required=True)
    parser.add_argument("--clarification", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    explanations = json.loads((args.evidence / "policy-explanations.json").read_text(encoding="utf-8"))
    turns = json.loads((args.evidence / "summary.json").read_text(encoding="utf-8"))["turns"]
    stages = json.loads((args.evidence / "policy-stages.json").read_text(encoding="utf-8"))
    record = explanations[args.turn - 1]
    proposal = next(stage["proposal"] for stage in stages
                    if stage["turn"] == args.turn and stage["stage"] == "PolicyAnswer")
    if proposal["decision"] != "clarify":
        raise ValueError("Selected turn has no initial clarification")
    payload = {**record["test_request"], "user_request": turns[args.turn - 1]["question"],
               "sources": record["sources"], "proposal": proposal}
    config = replace(Settings.from_env(), langfuse_enabled=False)
    factory = lambda role: budgeted_model(create_model(config), config, role)
    results = []
    for label, question, clarification in (
        ("baseline", payload["question"], proposal["question"]),
        ("only_remove_rewrite", args.question_without_rewrite, proposal["question"]),
        ("only_remove_repeated_condition", payload["question"], args.clarification),
    ):
        current = deepcopy(payload)
        current["question"] = question
        current["proposal"]["question"] = clarification
        review = _submit(factory, "policy_reviewer", PolicyAnswerReview,
                         _review_prompt("clarify"), current)
        results.append({"case": label, "input": current, "review": review.model_dump(mode="json")})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(label, review.model_dump(mode="json"), flush=True)


if __name__ == "__main__":
    main()
