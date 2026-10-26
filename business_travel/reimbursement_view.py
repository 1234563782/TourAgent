"""Conclusion-first rendering of source-bound expense projections."""

from decimal import Decimal

from .presentation import detail_block, single_line, table_row
from .result_labels import KIND_LABELS, policy_reason_label

REASONS = {
    "invalid_stay_period": "请核实入住、退房日期及测算天数范围。",
    "stay_period_required": "请补充退房日期，以便逐晚核对。",
    "nightly_amounts_conflict": "逐晚金额、日期与住宿总额不一致，请核实。",
    "expense_amount_scope_conflict": "费用科目与金额范围不一致，请核实。",
    "transport_breakdown_conflict": "票面费用、支付总额与金额范围不一致，请核实。",
    "expense_outside_published_period": "费用日期超出当前已发布制度的适用期间，需核对适用版本。",
    "expense_period_too_long": "起止日期跨度过长，请核实出差区间。",
    "estimate_evidence_incomplete": "未取得完整的同版本制度原文或附表，暂不展示金额结论。",
}


def render_estimates(data, field_labels, render_context):
    lines = ["### 费用报销测算"]
    details, evidence, registry = [], [], {}

    def register(result, items):
        sources = {source["chunk"]: source for source in result.get("sources", []) if source.get("chunk")}
        references = {}
        for item in items:
            for ident in item.get("evidence_ids", []) if item.get("evidence_verified") else []:
                source = sources.get(ident)
                if not source:
                    continue
                key = tuple(str(source.get(key) or "") for key in
                            ("file_sha256", "source", "version", "section", "clause", "text"))
                if key not in registry:
                    label = f"来源{len(registry) + 1}"
                    registry[key] = label
                    evidence.append((label, source))
                references[ident] = registry[key]
        return references

    def cite(item, refs):
        return " ".join(f"[{label}]" for label in dict.fromkeys(
            refs[ident] for ident in item.get("primary_evidence_ids", item.get("evidence_ids", [])) if ident in refs))

    for check in data.get("rule_checks", []):
        facts, result = check.get("facts", {}), check.get("result", {})
        estimate = result.get("estimate") or {}
        items = estimate.get("items", [])
        refs = register(result, items)
        policy_currency = single_line(estimate.get("policy_currency") or "")
        currency = single_line(estimate.get("currency") or policy_currency or "币种待确认")
        kind = KIND_LABELS.get(facts.get("kind"), "费用")
        hypothetical = estimate.get("scenario") == "hypothetical"
        lines.append(f"\n{kind} · {'假设测算' if hypothetical else '实际费用测算'}")
        if estimate.get("reference_amount") is not None:
            lines.append(f"按本次已核对的一般标准，可报参考金额：{estimate['reference_amount']} {currency}。")
        elif estimate.get("known_reference_amount") is not None:
            lines.append(f"已完成测算部分的可报参考金额：{estimate['known_reference_amount']} {currency}；完整金额待核定。")
        elif result.get("status") == "over_standard":
            lines.append("费用超出一般标准，最终可报金额待核定。")
        else:
            lines.append("暂不能确定完整可报金额。")
        if estimate.get("submitted_amount") is not None:
            lines.append(f"{'假设支出' if hypothetical else '提交费用'}：{estimate['submitted_amount']} {currency}。")
        if items:
            hotel = facts.get("kind") == "hotel"
            allowance = facts.get("kind") in {"meal", "local_transport"}
            header = (["日期", f"费用（{currency}）", f"住宿限额（{policy_currency}）",
                       f"可报参考金额（{currency}）", "超出限额", "依据"] if hotel else
                      ["日期", f"包干标准（{policy_currency}）", f"可报参考金额（{currency}）",
                       "状态", "依据"] if allowance else
                      ["日期", "席别", f"票面费用（{currency}）", f"可报参考金额（{currency}）", "依据"])
            lines.extend(["", table_row(header), table_row(["---"] * len(header))])
            for item in items:
                verified = item.get("evidence_verified")
                amount = item.get("amount") if item.get("amount") is not None else "待补充"
                reference = item.get("reference_amount") if item.get("reference_amount") is not None else "待核定"
                basis = cite(item, refs) if verified else "依据待核实"
                if hotel:
                    row = [item["date"], amount, item.get("cap") if item.get("cap") is not None else "-",
                           reference, item.get("excess") if item.get("excess") is not None else "-", basis]
                elif allowance:
                    state = {"within_checked_rules": "已核", "insufficient_information": "信息不足",
                             "manual_review": "转人工"}.get(item.get("status"), "待核定")
                    label = item["date"]
                    if item.get("addon"):
                        label = f"{label} 在途加发（{item.get('travel_hours')}小时）"
                    row = [label, item.get("cap") if item.get("cap") is not None else "-",
                           reference, state, basis]
                else:
                    row = [item["date"], facts.get("seat_class") or "待补充", amount, reference, basis]
                lines.append(table_row(row))
            units = {item.get("limit_unit") for item in items if item.get("cap") is not None}
            unit_label = {"person_day": "每人每天", "room_night": "每间每晚"}.get(
                next(iter(units)) if len(units) == 1 else None, "制度规定的单位")
            if hotel:
                lines.append(f"住宿限额按{unit_label}核对。")
            elif allowance:
                lines.append("包干标准按出差自然（日历）天数计算；会议或培训由举办方供餐的，仅计在途日。")
                if any(item.get("addon") for item in items):
                    lines.append("在途加发只按同一张票据的出发、到达时间计算，每满12小时加发一天，"
                                 "不按里程、行程时长或联程票据推算。")
            else:
                lines.append("本次测算票面费用。")
            if facts.get("currency") and facts["currency"] != policy_currency:
                details.append("费用币种与政策限额币种不同，本次未自动换算或计算差额。")
        if estimate.get("excess") is not None and any(item.get("status") == "over_standard" for item in items):
            lines.append(f"超出限额合计：{estimate['excess']} {currency}。")
            if estimate.get("standard_portion") is not None:
                details.append(f"标准内部分的金额拆分为 {estimate['standard_portion']} {currency}，"
                               "这只是与限额的数学比较，不等于超标后可按此金额报销；超标处理需核对例外条款及审批。")
        if estimate.get("unassessed_amount") is not None and Decimal(estimate["unassessed_amount"]) > 0:
            lines.append(f"尚未测算的其他费用：{estimate['unassessed_amount']} {currency}。")
            details.append("混合支付中的附加费、保险、退改费等需逐项提供金额和条件，本版不把整笔支付视作票面费用。")
        missing = result.get("missing_fields", [])
        if missing:
            details.append("请补充或核实：" + "、".join(field_labels.get(field, field) for field in missing) + "。")
        for code in estimate.get("assumptions") or []:
            details.append(ASSUMPTIONS.get(code, code))
        reason = result.get("reason")
        if reason and reason != "expense_amount_projection":
            details.append(REASONS.get(reason, policy_reason_label(reason)))
        for item in items:
            if item.get("status") not in {"within_checked_rules", "over_standard"}:
                reason = item.get("reason")
                if reason and reason != "required_facts_missing":
                    details.append(REASONS.get(reason, policy_reason_label(reason)))
        if refs:
            details.append("本项测算条款：" + " ".join(f"[{label}]" for label in dict.fromkeys(refs.values())))
        context = render_context(check.get("fact_context", {}), compact=True)
        if context:
            details.append(detail_block("查看本次费用条件", context))
    lines.append("\n### 补充说明")
    lines.extend(dict.fromkeys(details))
    lines.append("以上金额以所填条件和当前已发布制度为前提；实际报销仍需有效票据及适用审批，最终金额由财务核定。")
    for label, source in evidence:
        location = " / ".join(single_line(source[key]) for key in
                              ("source", "version", "clause", "section", "sheet", "cell_range") if source.get(key))
        lines.append(detail_block(f"政策依据 · {label}", f"{location}\n原文摘录：\n{source['text']}"))
    return "\n".join(lines)


CLEARED_STATUSES = {"within_checked_rules"}

ASSUMPTIONS = {
    "currency_cny_assumed": "未注明币种，按人民币核对。",
    "tax_inclusive_assumed": "未注明是否含税，按含税价核对。",
    "date_not_compared_with_receipt": "日期按你所述核对，未与票据比对。",
}


def render_batch_summary(lines):
    """Total only when every participating line is cleared; otherwise list cleared lines alone."""
    if len(lines) < 2:
        return ""
    actual = [line for line in lines if line.get("scenario") != "hypothetical"]
    assumed = [line for line in lines if line.get("scenario") == "hypothetical"]
    currency = next((line.get("currency") for line in actual if line.get("currency")), "")
    cleared, blocked = [], []
    for line in actual:
        if (line.get("reference_amount") is not None and line.get("evidence_complete")
                and line.get("status") in CLEARED_STATUSES):
            cleared.append(line)
        else:
            blocked.append(line)
    cleared_ids = {id(line) for line in cleared}
    out = ["### 本次核对汇总"]
    if actual and not blocked:
        total = sum((Decimal(line["reference_amount"]) for line in cleared), Decimal(0))
        out.append(f"已核参考合计：{total} {currency}。".strip())
        out.append(f"参与本次核对的 {len(actual)} 行均已核清且依据完整。")
    elif actual:
        out.append(f"本次未给出合计：{len(blocked)} 行未核清。已核清的行单独列示，不并入合计。")
    else:
        out.append("本次只有假设测算，未给出实际合计。")
    out.extend(["", table_row(["科目", "核对结果", "可报参考金额"]),
                table_row(["---"] * 3)])
    for line in actual:
        label = KIND_LABELS.get(line.get("kind"), "费用")
        if id(line) in cleared_ids:
            out.append(table_row([label, "已核", f"{line['reference_amount']} {currency}".strip()]))
        elif line.get("status") == "over_standard":
            out.append(table_row([label, "超出标准，未计入合计", "待核定"]))
        else:
            state = "需人工核验" if line.get("status") == "manual_review" else "信息不足，未核清"
            out.append(table_row([label, state, "待核定"]))
    if assumed:
        out.extend(["", f"另有 {len(assumed)} 行假设测算，仅作参考，不计入实际合计。"])
    return "\n".join(out)

