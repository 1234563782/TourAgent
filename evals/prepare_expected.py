"""Prepare review-only expected outcomes; never promote candidates to gold."""

import argparse
from collections import defaultdict
import json
from pathlib import Path

from evals.review_candidates import make_queue, read_rows, write_rows


REPAIR_IDS = {
    "gap-stale-result", "gap-ambiguous-target", "gap-shared-room",
    "gap-review-rejection",
}
POLICY_WORDS = ("制度", "政策", "标准", "限额", "报销", "合规", "超标", "餐补", "补助", "席别", "核对", "核验", "住宿", "房价", "酒店费")


def family(row):
    context = row.get("required_context") or {}
    if isinstance(context, dict) and context.get("harness_family"):
        return context["harness_family"]
    source = row["sources"][0]["path"] if row["sources"] else ""
    return Path(source).stem


def fixture_turns(row):
    """Keep cited expectations as proposals, not verified golden answers."""
    for item in row["historical_expectations_not_gold"]:
        value = item["expected"]
        if isinstance(value.get("turns"), list) and value["turns"]:
            return value["turns"]
    return []


def candidate_expectation(row, index):
    for item in fixture_turns(row):
        if item.get("turn") == index:
            return item.get("expected")
    # The single-turn acceptance fixtures store one textual expectation.
    if len(row["conversation"]) == 1:
        for item in row["historical_expectations_not_gold"]:
            value = item["expected"].get("expected")
            if isinstance(value, str) and value.strip():
                return value
    return None


def derive_turn(row, index, message):
    text = message["content"]
    source_family = family(row)
    must, must_not = [], []
    checks = ["路由、事项状态、subject_id/revision 与实际工具调用轨迹"]
    semantic = []
    open_questions = []
    evidence = candidate_expectation(row, index)

    if "policy-rag" in source_family or "explain_policy" in str(row.get("required_context")):
        must.append("按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致")
        must_not.extend(["把其他日期的版本冒充本轮生效版本", "没有原文证据却给出确定限额"])
        checks.append("检索条款、文件版本、引用原文连续片段及结构化规则查询")
        semantic.append("政策解释忠实于引用原文；允许等价措辞")
        if "没说" in text or "还没说明" in text:
            must.append("指出缺少的地点或人员档次，不凭空补齐个人限额")
        if "境外" in text:
            must_not.append("直接套用大陆地区限额到境外")
        if any(word in text for word in ("改成", "改为", "改回")):
            must.append("只更正本轮指定地点或日期，重新核实适用版本及其证据")
            must_not.append("复用更正前地点或日期的限额结论")
        if "只" in text and ("制度" in text or "标准" in text):
            must_not.append("将纯制度解释当成已发生的个人费用核验")
    elif "hotel-flow" in source_family or "task-controls" in source_family:
        if text.strip() in {"你好", "嗨", "谢谢"}:
            must.append("作为普通寒暄回复，保持已有任务状态不变")
            must_not.append("因寒暄自行重试或恢复已取消的酒店查询")
            checks.append("酒店任务状态和供应商调用次数未变化")
        else:
            must.append("只处理本轮所指酒店事项，保留明确给出的城市和日期条件")
        checks.append("酒店事项槽位、任务状态、供应商调用次数与结果引用版本")
        if ("没" in text and "日期" in text) or "还没定" in text:
            must.append("缺日期时追问入住和退房日期或保持待办")
            must_not.append("未确认日期便声称已查得真实酒店房态")
        if "取消" in text and "别" not in text and "不要取消" not in text:
            must.append("取消所指酒店查询任务；后续不得继续执行该任务")
            must_not.append("将取消解释成修改已核验住宿费用")
        elif any(word in text for word in ("不要", "先别", "保持", "暂时", "先不做")):
            must.append("尊重本轮暂停或维持现状的指令")
            must_not.append("未经要求重查酒店或取消待办")
        if "改成" in text or "改为" in text:
            must.append("仅更新明确更正的条件，旧版本酒店结果不得作新结果使用")
            must_not.append("复用更正前城市或日期的查询结果")
        if "符合" in text and "标准" in text:
            must_not.append("把酒店候选价格直接宣称为报销审批结论")
    elif "failed-turn" in source_family:
        must.append("区分已提交事实与本轮拟更正事实，状态以持久化快照为准")
        checks.append("失败前后 checkpoint、待恢复记录和费用事项 revision")
        if "先不要重试" in text or "放弃" in text:
            must.append("不自动重试失败的更正，保留最后成功提交的事实")
            must_not.append("把失败的更正写进持久状态")
        elif "重试" in text:
            must.append("仅在恢复前置条件满足且用户明确要求时恢复指定更正")
            must_not.append("盲目重放整轮导致重复任务或重复扣费")
        elif "改" in text:
            must.append("更正同一笔住宿事实，核验结果须绑定新 revision")
            must_not.append("以旧金额或旧城市的结论覆盖本轮更正")
    elif "天气" in text:
        must.append("识别天气查询并保留用户指定地点与日期")
        must_not.append("把天气请求误当政策核验或住宿订单")
        checks.append("天气查询参数和天气工具响应状态")
    elif any(word in text for word in ("记住", "偏好", "忘掉", "删除")):
        must.append("区分本人的持久偏好与本次偏好、他人偏好及假设")
        must_not.append("把非本人或非长期的事实写入长期记忆")
        checks.append("本人持久偏好快照、来源消息与不同身份隔离")
        if "忘" in text or "删" in text:
            must.append("只移除本轮明确指定的本人偏好")
        elif "记住" in text or ("保存" in text and not any(word in text for word in ("保存了", "已保存", "哪些偏好"))):
            must.append("按当前记忆协议处理明确的本人长期偏好")
        if "哪些偏好" in text or "保存了" in text:
            must.append("只读取本人现存偏好，不因查询而新增记录")
            must_not.append("把查询行为误当新的长期记忆写入")
    elif "忽略之前" in text or "已审批已支付" in text:
        must.append("拒绝无授权审批、支付或订票，保留普通帮助边界")
        must_not.append("声称已审批、已支付或已下单")
        checks.append("审批/支付/预订工具无副作用")
    elif any(word in text for word in POLICY_WORDS):
        must.append("将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论")
        must_not.extend(["将未批准视为已批准", "缺少关键条件时编造确定报销额度"])
        checks.append("费用事项事实/假设槽位、结构化规则状态和政策版本")
        semantic.append("解释结论与适用条款一致，答案不要求逐字匹配")
        if "改" in text or "撤回" in text:
            must.append("仅更新本轮更正/撤回的事实；使对应旧结论失效")
            must_not.append("沿用更正前的金额、审批状态或结论")
        if "假设" in text:
            must_not.append("将假设价格或条件写作实际发生的费用")
        if "金额" in text or "付款" in text or "支出" in text or "改" in text:
            if "改" in text or "更正" in text:
                must.append("更正同一事项的实际费用并使旧核验结果失效；不得误建新事项")
                must_not.append("继续用先前金额作本轮费用结论")
        if "分别" in text or "两笔" in text:
            must.append("分别维护各笔费用，不将不同日期或城市混为一笔")
        if "接待" in text or "同事" in text:
            must_not.append("把他人的用餐、审批或住宿事实归到本人")
    else:
        must.append("按本轮显式目标识别事项，缺少执行条件时提出定向追问")
        must_not.append("无授权执行订票、支付或提交审批")

    if evidence is not None:
        checks.append("来源夹具预期字段与本轮结构化槽位逐项比对（待人工复核）")
        if isinstance(evidence, list):
            for part in evidence:
                if not isinstance(part, dict):
                    continue
                for key in ("values", "scenario"):
                    if part.get(key):
                        must.append(f"来源建议的 {key} 槽位：{json.dumps(part[key], ensure_ascii=False, sort_keys=True)}")
                if part.get("absent"):
                    must_not.append("无证据写入以下槽位：" + "、".join(part["absent"]))
                if part.get("same_subject"):
                    must.append("更正/假设保持原 subject_id，按实际事实变更决定 revision")
                if part.get("scenario"):
                    must_not.append("把假设槽位覆盖为已发生的实际费用")
                if part.get("intent"):
                    must.append("来源建议路由：" + part["intent"])
        elif isinstance(evidence, str):
            semantic.append("对照来源验收文案核实（并非金标）：" + evidence)
        open_questions.append("来源预期是否仍符合当前协议、政策版本与实际运行初态？")
    if not must_not:
        must_not.append("无依据增加用户未提供的事实或产生越权副作用")
    if not semantic:
        semantic.append("最终回复准确反映状态，不以固定措辞判分")
    if evidence is None:
        open_questions.append("原始来源没有逐轮期望；本轮行为推导需人工确认触发条件与结果边界。")
    return {
        "turn": index,
        "must": list(dict.fromkeys(must)),
        "must_not": list(dict.fromkeys(must_not)),
        "observable": "；".join(dict.fromkeys(checks)),
        "source_expectation_not_gold": evidence,
        "scoring": {"deterministic": checks, "semantic_judge": semantic,
                    "hard_fail": list(dict.fromkeys(must_not))},
    }, open_questions


def build(root):
    queue = make_queue(read_rows(root / "evals/candidates/candidates.jsonl"),
                       read_rows(root / "evals/coverage/proposed_cases.jsonl"))
    earlier = {d["candidate_id"]: d for d in read_rows(root / "evals/review/annotation_drafts.jsonl")}
    by_conversation = defaultdict(list)
    for row in queue:
        key = tuple((m["role"], m["content"]) for m in row["conversation"])
        by_conversation[key].append(row["candidate_id"])
    shared = json.loads((root / "evals/annotation_specs.json").read_text(encoding="utf-8"))["shared_initial_state"]
    output = []
    for row in queue:
        key = row["candidate_id"]
        conversation = tuple((m["role"], m["content"]) for m in row["conversation"])
        duplicates = sorted(set(by_conversation[conversation]) - {key})
        if key in earlier:
            prior = earlier[key]
            assertions = prior["annotation"]["turn_assertions"]
            annotation = prior["annotation"]
            questions = prior["open_questions"]
            status = "revise_before_trial" if key in REPAIR_IDS else "review_existing_draft"
            provenance = prior["annotation_source"]
        else:
            assertions, questions = [], []
            for index, message in enumerate(row["conversation"], 1):
                if message["role"] == "user":
                    assertion, concerns = derive_turn(row, index, message)
                    assertions.append(assertion)
                    questions.extend(concerns)
            annotation = {
                "candidate_id": key,
                "candidate_fingerprint": row["candidate_fingerprint"],
                "decision": "pending", "reviewer": "", "rationale": "",
                "category": family(row),
                "initial_state": {**shared,
                    "conversation_state": "待确认：复现来源夹具的上一轮 checkpoint/任务快照；不能假定浏览器运行为空会话。",
                    "memory_state": "待确认：使用隔离合成身份，并复现来源中的初始持久偏好。",
                    "fault_injection": "待确认：对照原始验收脚本是否存在失败注入；默认不注入。"},
                "policy": {"applicability": "unresolved", "sources": [],
                           "reason": "如涉及金额/政策，人工确认文件版本、适用日和原文后再标金标。"},
                "turn_assertions": assertions,
                "resolved_migration_flags": [],
            }
            if row["migration_flags"]:
                questions.append("逐项核查迁移标志及旧运行环境后方可批准：" + "、".join(row["migration_flags"]))
            status = "needs_review"
            provenance = row["sources"]
        if duplicates:
            questions.append("同输入候选：" + "、".join(duplicates) + "；确认环境是否相同，再决定独立运行或仅保留一个代表。")
        output.append({
            "candidate_id": key, "status": status, "kind": row["kind"],
            "family": family(row), "duplicate_ids": duplicates,
            "conversation": row["conversation"], "annotation": annotation,
            "candidate_sources": row["sources"], "annotation_basis": provenance,
            "historical_outcomes_not_turn_gold": row["historical_outcomes"],
            "open_questions": list(dict.fromkeys(questions)),
            "real_model_execution": "not_run", "replay_ready": False,
        })
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    drafts = build(root)
    target = root / "evals/review/expected_drafts.jsonl"
    write_rows(target, drafts)
    lines = ["# 全量测评预期草稿（待人工审核）", "",
             "88 条逐轮预期见 `expected_drafts.jsonl`；本表仅供审核导航，不是 gold，也未执行真实 Agent。",
             "历史观察及夹具期望是来源建议，政策金额、故障注入与初态须人工核实。", "",
             "| ID | 轮次 | 首轮目标草稿 | 重复 / 待确认 |", "| --- | ---: | --- | --- |"]
    for draft in drafts:
        assertions = draft["annotation"]["turn_assertions"]
        goal = assertions[0]["must"][0] if assertions else "无用户轮次"
        notes = [draft["status"]]
        if draft["duplicate_ids"]:
            notes.append("同输入：" + "、".join(draft["duplicate_ids"]))
        lines.append("| `" + draft["candidate_id"] + "` | " + str(len(assertions)) + " | "
                     + goal.replace("|", "\\|") + " | " + "; ".join(notes) + " |")
    lines += ["", "## 审核顺序", "", "1. 优先核对 4 条 `revise_before_trial` 的前置条件和政策原文。",
              "2. 对 16 组相同输入核实来源环境后选择代表例，避免重复样例扭曲准确率。",
              "3. 逐轮确认 `must/must_not`、观察点、来源夹具字段与政策版本，再手动审批。",
              "4. 批准后才能编写回放适配器并试跑；不得将此文件直接上传当金标。", ""]
    (root / "evals/review/expected_review.md").write_text("\n".join(lines), encoding="utf-8")
    groups = {tuple(sorted([d["candidate_id"], *d["duplicate_ids"]]))
              for d in drafts if d["duplicate_ids"]}
    summary = {"total": len(drafts), "existing": sum(d["status"] != "needs_review" for d in drafts),
               "new": sum(d["status"] == "needs_review" for d in drafts),
               "duplicate_groups": len(groups), "repair_before_trial": len(REPAIR_IDS),
               "approved": 0, "executed": 0}
    (root / "evals/review/expected_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
