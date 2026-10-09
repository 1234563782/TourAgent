"""显式联网的政策回答验收：只读政策库，不写用户会话，输出到指定文件。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from langchain_core.callbacks import BaseCallbackHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_travel.models import create_model
from business_travel.policy_guard import render_policy
from business_travel.policy_pg import PostgresPolicyStore
from business_travel.policy_rag import explain_policy
from business_travel.settings import Settings


class ProposalRecorder(BaseCallbackHandler):
    """仅用于显式启用的合成问题验收，不保存请求头或配置。"""

    def __init__(self):
        self.proposals = []

    def on_llm_end(self, response, **kwargs):
        for batch in response.generations:
            for generation in batch:
                message = getattr(generation, "message", None)
                for call in getattr(message, "tool_calls", []):
                    self.proposals.append({"name": call["name"], "args": call["args"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--facts", required=True, help="JSON 对象，仅使用测试事实，勿含个人隐私")
    parser.add_argument("--category", required=True,
                        choices=["hotel", "transport", "meal", "local_transport", "other"])
    parser.add_argument("--mode", default="personal_limit", choices=["general", "personal_limit"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--capture-proposals", action="store_true",
                        help="仅对无隐私的合成问题启用：将模型草稿保存在本地验收文件")
    args = parser.parse_args()
    try:
        facts = json.loads(args.facts)
        if not isinstance(facts, dict):
            raise ValueError("Facts must be an object")
        if args.output.exists():
            raise FileExistsError("Output already exists")
        config = replace(Settings.from_env(), langfuse_enabled=False)
        missing = config.missing_model_config() + config.missing_policy_config()
        if missing:
            print(json.dumps({"status": "unavailable", "missing_variables": missing}))
            return 2
        recorder = ProposalRecorder()

        def model_factory(role):
            model = create_model(config)
            if args.capture_proposals:
                model.callbacks = [recorder]
            return model

        data = explain_policy(PostgresPolicyStore(config), model_factory,
            config, args.question, facts=facts,
            policy_query={"category": args.category, "mode": args.mode})
        summary = {"question": args.question, "facts": facts, "rag": data["rag"],
                   "source_chunks": [s["chunk"] for s in data["sources"]],
                   "answer": render_policy(data), "status": data["status"]}
        if args.capture_proposals:
            summary["model_proposals"] = recorder.proposals
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(summary, target, ensure_ascii=False, indent=2)
        print(json.dumps({"status": data["rag"]["status"], "retrievals": data["rag"]["retrievals"],
                          "stop_reason": data["rag"].get("stop_reason"),
                          "diagnostic": data["rag"].get("diagnostic"),
                          "output": str(args.output)}, ensure_ascii=False))
        return 0 if data["rag"]["status"] in {"answered", "partial", "clarify"} else 1
    except Exception as error:
        print(json.dumps({"status": "error", "category": type(error).__name__,
                          "reason": "验收未完成；未输出凭据或敏感异常正文。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
