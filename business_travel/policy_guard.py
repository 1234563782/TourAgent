"""User-evidence checks and deterministic presentation of policy tool results."""

import json

from pydantic import Field

from .contracts import Contract
from .result_labels import KIND_LABELS, policy_reason_label
from .presentation import detail_block, table_row


class FactEvidence(Contract):
    user_message: int = Field(ge=0, description="Zero-based index in user_messages; never cite assistant/tool text.")
    quote: str = Field(min_length=1, max_length=2000,
                       description="Shortest exact user quote supporting this field, not an inference.")


FIELD_LABELS = {
    "personnel_type": "人员身份", "academic_title": "职称", "technical_grade": "专业技术岗位等级",
    "management_grade": "管理岗位等级", "transport_standard": "适用交通类别",
    "lodging_standard": "住宿人员档次", "destination_province": "目的地所属省级地区",
    "trip_context": "出差场景", "student_activity_eligible": "是否属于规定的学生活动",
    "organizer_provides_meals": "会议或培训是否统一供餐", "in_transit_day": "是否在途日期",
    "local_transport_method": "市内交通选择包干还是凭据", "paid_by_other_organization": "校外单位是否承担市内交通",
    "organizer_arranged_lodging": "是否主办方统一安排且费用自理的会议培训住宿",
    "organizer_lodging_proof": "是否有主办单位有效证明",
    "query_scope": "查询范围", "journey": "单程或往返",
    "kind": "费用科目", "origin": "出发地", "departure_date": "出发日期",
    "return_date": "返程日期", "check_in": "入住日期", "check_out": "退房日期",
    "purpose": "出差目的", "meeting_address": "办事地址", "transport": "交通方式",
    "train_service": "列车类型", "budget": "预算", "room_occupants": "入住人数",
    "hotel_rooms": "酒店房间总数", "hotel_adults": "成人总人数", "hotel_child_ages": "儿童年龄列表",
    "expense_date": "费用发生日期", "expense_end": "费用区间结束日期",
    "in_transit_dates": "在途日期", "destination": "目的地", "employee_grade": "职级",
    "mainland_business_trip": "是否大陆因公出差", "long_assignment_days": "本次出差或连续驻场总天数",
    "special_approval": "是否有专项审批", "amount": "实际金额", "currency": "币种",
    "basis": "住宿计价口径（每人每天或每间每晚）",
    "basis": "计价单位", "taxes_confirmed": "是否含税", "date_verified": "日期是否已核实",
    "occupants": "入住人数", "roommate_grade": "同住人职级", "voluntary_twin_share": "是否双方自愿合住",
    "transport_mode": "交通方式", "seat_class": "席别", "suitable_direct_train_hours": "适用直达高铁时长",
    "flight_approval": "是否获准乘机", "overnight_trip": "是否过夜", "allowance_region": "补助地区",
    "covered_meals": "免费提供的餐次", "local_business_travel": "是否有市内公务出行",
    "local_transport_coverage": "接送覆盖范围", "self_paid_local_travel": "是否自行承担市内公务交通",
    "local_car_cost_reimbursed": "是否另报私车费用",
    "expense_scenario": "实际或假设费用", "amount_scope": "单晚、每晚同价或整段总额",
    "nightly_amounts": "逐晚金额和日期", "transport_amount_scope": "票面费用或含附加费总额",
    "fare_amount": "票面费用",
}


def render_fact_context(context, *, compact=False):
    fields = context.get("fields", {})
    if not fields:
        return ""
    labels = {
        "query_scope": {"hotel": "仅酒店", "transport": "仅交通", "combined": "交通和酒店"},
        "journey": {"one_way": "单程", "round_trip": "往返"},
        "covered_meals": {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐"},
        "kind": {"hotel": "住宿", "meal": "餐补", "transport": "交通", "local_transport": "市内交通"},
        "basis": {"room_night": "每间每晚", "person_day": "每人每天"},
        "expense_scenario": {"actual": "实际发生", "hypothetical": "假设测算"},
        "amount_scope": {"single": "单晚或单张票", "uniform_nightly": "明确每晚同价", "stay_total": "住宿总额"},
        "transport_amount_scope": {"fare_only": "仅票面费用", "total": "含其他费用总额", "unknown": "待确认"},
        "personnel_type": {"staff": "教职工", "student": "学生", "invited": "受邀人员"},
        "trip_context": {"ordinary": "普通出差", "conference": "会议", "training": "培训", "research": "科研",
                         "fieldwork": "野外作业", "self_drive": "自驾", "rental": "租车", "noon_rest": "午休房"},
        "local_transport_method": {"allowance": "包干", "receipts": "凭据报销"},
        "transport": {"flight": "飞机", "train": "火车", "compare": "比较飞机与火车"},
    }

    def display(value, field):
        if value is None:
            return "未提供"
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, list):
            return "、".join(display(item, field) for item in value) or "无"
        if isinstance(value, str):
            return labels.get(field, {}).get(value, value)
        return json.dumps(value, ensure_ascii=False)

    lines = ["按你提供的信息整理：" if compact else
             "我识别到的信息（根据你当前的表述整理，不代表业务系统已核实，也不代表学校系统已核实）："]
    for name, record in fields.items():
        if name == "lodging_standard":
            # Never surface a lodging grade the user did not ask about.
            continue
        label = FIELD_LABELS.get(name, name)
        if name == "amount" and fields.get("expense_scenario", {}).get("value") == "hypothetical":
            label = "假设金额"
        lines.append(f"- {label}：{display(record.get('value'), name)}")
    lines.append("如需修改行程信息，直接告诉我即可。" if compact else
                 "我会按以上信息继续查询或核对；如果有任何信息需要修改，直接告诉我即可。")
    return "\n".join(lines)


def source_labels(sources):
    return list(dict.fromkeys(
        " / ".join(str(source.get(key) or "") for key in
                   ("source", "version", "clause", "sheet", "cell_range")).strip(" /")
        for source in sources if source.get("source")
    ))


def _public_source_label(source):
    return " / ".join(str(source[key]) for key in
                      ("source", "version", "clause", "section", "sheet", "cell_range") if source.get(key))


def _public_source_refs(used_ids):
    return {ident: f"来源{index}" for index, ident in enumerate(dict.fromkeys(used_ids), start=1)}


def _policy_failure_message(diagnostic):
    from .models import safe_model_error_message

    stage = diagnostic.get("stage")
    code = diagnostic.get("failure_code")
    if stage == "policy_retrieve":
        return "政策资料检索失败，暂时无法取得依据，请稍后重试。"
    if code == "unverified_policy_citation":
        return "已检索到政策资料，但回答中的引用未通过核对，本次未展示该回答，请重试。"
    if code in {"invalid_policy_tool_arguments", "invalid_policy_tool_response",
                "clarification_must_request_only_missing_fields"}:
        return "已检索到政策资料，但模型回答格式校验失败，本次未展示该回答，请重试。"
    if stage in {"policy_generate", "policy_review"}:
        return "已检索到政策资料，但回答生成或复核未完成。" + safe_model_error_message(diagnostic)
    return "政策回答暂不可用，本次未输出未经校验的结论，请稍后重试。"


def render_policy(data):
    if data.get("check_mode") == "estimate" and any(
            isinstance(check.get("result", {}).get("estimate"), dict) for check in data.get("rule_checks", [])):
        from .reimbursement_view import render_estimates
        return render_estimates(data, FIELD_LABELS, render_fact_context)
    lines = ["政策核对结果（不是审批或报销批准）："]
    rag = data.get("rag")
    if data.get("purpose") == "explain" and isinstance(rag, dict):
        status = rag.get("status")
        if status in {"answered", "partial"}:
            lodging_table = data.get("lodging_table")
            if status == "answered" and isinstance(lodging_table, dict):
                rows = lodging_table.get("rows", [])
                standard = lodging_table.get("standard")
                grades = lodging_table.get("grades") or []
                source_refs = _public_source_refs(
                    [row.get("source_ref") for row in rows if row.get("source_ref")])
                lines = ["### 政策解答"]
                if standard:
                    lines.append(f"按已确定的住宿人员档次“{standard}”列出住宿费标准；已确认的身份和目的地事实仍保留，本次只扩大回答范围。")
                    header = ["地区", "适用地区", "平季标准", "旺季标准/期间"]
                    body = []
                    for row in rows:
                        seasonal = ""
                        if row.get("seasonal_cap"):
                            seasonal = f"{row['seasonal_cap']}（{row.get('season_text') or '旺季'}）"
                        ref = source_refs.get(row.get("source_ref"), "")
                        body.append(table_row([
                            row.get("region", ""), "、".join(row.get("localities") or []),
                            row.get("ordinary_cap", ""), seasonal + (f" [{ref}]" if ref else "")
                        ]))
                    lines.extend([f"单位：{lodging_table.get('currency', 'CNY')} / 人·天。",
                                  table_row(header), table_row(["---"] * len(header)), *body])
                else:
                    header = ["地区", "适用地区", *grades]
                    body = []
                    for row in rows:
                        ref = source_refs.get(row.get("source_ref"), "")
                        values = [row.get("region", ""), "、".join(row.get("localities") or [])]
                        values.extend((row.get("caps") or {}).get(grade, "") for grade in grades)
                        if ref:
                            values[-1] = f"{values[-1]} [{ref}]"
                        body.append(table_row(values))
                    lines.extend([f"单位：{lodging_table.get('currency', 'CNY')} / 人·天。",
                                  table_row(header), table_row(["---"] * len(header)), *body])
                lines.append("\n以上为政策解释，不是审批或报销批准。")
                evidence = []
                source_by_ref = {source.get("source_id"): source for source in data.get("sources", [])}
                for source_id, label in source_refs.items():
                    source = source_by_ref.get(source_id)
                    if not source:
                        continue
                    source_label = ("配置摘录（项目已审核配置，非制度原文）"
                                    if source.get("source_type") == "reviewed_rule" else "原文摘录")
                    evidence.append(f"[{label}] {_public_source_label(source)}\n\n{source_label}：\n{source.get('text', '')}")
                if evidence:
                    lines.append("\n" + detail_block("政策依据", "\n\n".join(evidence)))
                return "\n".join(lines)
            proposal = rag["proposal"]
            lines = ["### 政策解答"]
            sources = {source["source_id"]: source for source in data["sources"]}
            claims, excerpts = [], {}
            for claim in proposal["claims"]:
                cited = []
                for citation in claim.get("citations", []):
                    source = sources.get(citation.get("source_id"))
                    quote = citation.get("quote")
                    if quote is None and source:
                        quote = source.get("text")
                    if (not source or not isinstance(quote, str) or not quote.strip()
                            or quote not in source.get("text", "")):
                        break
                    cited.append((citation["source_id"], quote))
                else:
                    if cited:
                        claims.append((claim["text"], list(dict.fromkeys(ident for ident, _ in cited))))
                        for ident, quote in cited:
                            excerpts.setdefault(ident, [])
                            if quote not in excerpts[ident]:
                                excerpts[ident].append(quote)
            if not claims:
                return "政策依据不完整，暂不能展示可靠结论，请重新查询。"
            public_refs = _public_source_refs(excerpts)
            for text, ids in claims:
                lines.append("\n" + text + " " + " ".join(f"[{public_refs[ident]}]" for ident in ids))
            if status == "partial":
                lines.append("\n### 还需核实")
                for item in proposal["limitations"]:
                    lines.append(f"\n{item['topic']}：{item['detail']}\n{item['next_step']}")
            lines.append("\n以上为政策解释，不是审批或报销批准。")
            if len(claims) != len(proposal["claims"]):
                lines.append("部分结论的引用无法核实，暂未展示。")
            for ident, quotes in excerpts.items():
                source = sources[ident]
                label = ("配置摘录（项目已审核配置，非制度原文）" if source.get("source_type") == "reviewed_rule"
                         else "原文摘录")
                lines.append("\n" + detail_block("政策依据 · " + public_refs[ident],
                    _public_source_label(source) + "\n\n" + label + "：\n" + "\n\n".join(quotes)))
        elif status == "clarify":
            lines.append(rag["proposal"]["question"])
        elif status == "unavailable" and isinstance(rag.get("diagnostic"), dict):
            lines.append(_policy_failure_message(rag["diagnostic"]))
        elif data.get("reason") == "policy_query_required":
            lines.append("这项政策问题的类别或目标尚未完整记录，请重新说明要查询的政策问题；"
                         "本次未猜测您的意思，也未给出适用标准。")
        else:
            lines.append({
                "incomplete": "检索依据不完整或超过上下文预算，暂不能给出完整结论。请缩小问题范围或核对完整制度。",
                "conflict": "检索到的制度依据存在冲突，暂不能确定适用口径，请由制度管理人员核实。",
                "version_changed": "检索期间政策版本发生变化或来源不一致，请重新查询，不能混用不同版本。",
                "unavailable": "政策检索或回答校验暂不可用，未输出未经校验的结论，请稍后重试。",
            }.get(status, "现有政策证据不足以可靠回答该问题，请补充适用条件或核对制度资料。"))
            if data.get("reason") == "no_published_policy":
                lines.append("当前尚无已发布政策，请联系管理员发布制度资料。")
        return "\n".join(lines)
    checks = data.get("rule_checks", [])
    if not checks:
        if data.get("purpose") == "explain" and data.get("sources"):
            if data.get("context_incomplete"):
                lines.append("检索依据不完整，以下原文不足以支持完整结论，请继续核对制度。")
            lines.append("以下为检索到的制度原文，仅解释标准，不判断个人费用是否合规：")
            seen = set()
            for source in data["sources"]:
                key = (source.get("source"), source.get("version"), source.get("clause"), source.get("text"))
                if source.get("text") and key not in seen:
                    seen.add(key)
                    labels = source_labels([source])
                    lines.extend([f"\n来源：{labels[0] if labels else '未标注'}", source["text"]])
            return "\n".join(lines)
        reasons = {
            "no_published_policy": "当前尚无已发布政策，请联系管理员发布制度资料。",
            "enterprise_policy_documents_unconfigured": "组织政策资料尚未配置。",
        }
        lines.append(reasons.get(data.get("reason"), "信息不足：本轮没有取得可用于个人费用判断的规则核验结果。"))
    for check in checks:
        facts, result = check.get("facts", {}), check.get("result", {})
        context = render_fact_context(check.get("fact_context", {}))
        if context:
            lines.append(context)
        lines.append(f"\n费用日期：{facts.get('expense_date', '待确认')}；"
                     f"科目：{KIND_LABELS.get(facts.get('kind'), '待确认')}")
        status, reason = result.get("status"), result.get("reason")
        if status == "insufficient_information":
            names = list(result.get("missing_fields", []))
            lines.append("信息不足，不能确认合规。")
            if names:
                lines.append("请补充或核实：" + "、".join(FIELD_LABELS.get(name, name) for name in names) + "。")
            if reason == "unsupported_user_facts":
                lines.append("上述字段未通过用户原话证据检查，未提交确定性规则引擎。")
        elif status == "manual_review":
            lines.append("需转人工核验，不能自动放行。")
            if reason == "long_assignment_requires_separate_plan":
                lines.append("长期驻场需要核对单独批准的驻场方案；目前不能据“无专项审批”判断有无驻场方案。")
            elif reason == "verify_special_approval_scope":
                lines.append("需要人工核实专项审批覆盖的人员、事项、期限和额度。")
        elif status == "over_standard":
            lines.append("超出所核对的一般标准，不等于已获特批。")
        elif status == "within_checked_rules":
            lines.append("在本次所核对的规则范围内；不代表完整合规或批准。")
        else:
            lines.append("未取得有效核验结果，不能确认合规。")
            if reason == "no_published_policy":
                lines.append("当前尚无已发布政策，请联系管理员发布制度资料。")
        if status in {"over_standard", "within_checked_rules"}:
            for key, label in (("cap", "适用限额"), ("amount", "核验金额"), ("excess", "超出金额")):
                if key in result:
                    unit = {"person_day": " / 人·天", "room_night": " / 间·晚"}.get(result.get("unit"), "")
                    lines.append(f"{label}：{result[key]} {result.get('currency', '')}{unit}".rstrip())
            if facts.get("seat_class"):
                lines.append("仅核对席别：" + facts["seat_class"])
        if reason:
            if reason == "no_published_policy" and status == "insufficient_information":
                lines.append("当前尚无已发布政策，请联系管理员发布制度资料。")
            lines.append("核验依据：" + policy_reason_label(reason))
    labels = source_labels(data.get("sources", []))
    if labels:
        lines.append("\n来源：\n" + "\n".join(labels))
    return "\n".join(lines)
