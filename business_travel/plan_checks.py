"""Validate supplier timestamps against the confirmed trip; never infer missing times."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from .policy_guard import FIELD_LABELS, render_fact_context
from .result_labels import KIND_LABELS, QUOTE_LABELS, POLICY_STATUS_LABELS, policy_reason_label
from .presentation import detail_block, single_line, table_row


def check_timeline(selected, trip):
    issues = []
    unknown = []
    outbound_arrival = None
    inbound_departure = None
    for item in selected:
        if item.kind == "hotel":
            if not trip.check_in or item.request_date != trip.check_in:
                issues.append("hotel_date_mismatch")
            continue
        inbound = bool(item.details.get("inbound"))
        day = trip.return_date if inbound else trip.departure_date
        try:
            departure = datetime.fromisoformat(item.departure or "")
            arrival = datetime.fromisoformat(item.arrival or "")
            if (departure.tzinfo is None) != (arrival.tzinfo is None):
                raise ValueError("Inconsistent timezone information")
        except (TypeError, ValueError):
            unknown.append(item.id)
            continue
        if day is None or departure.date() != day:
            issues.append("transport_departure_date_mismatch")
        if arrival <= departure:
            issues.append("arrival_not_after_departure")
        if inbound:
            inbound_departure = departure
            if trip.return_date and arrival.date() > trip.return_date:
                issues.append("overnight_return_requires_confirmation")
        else:
            outbound_arrival = arrival
            if trip.check_out and arrival.date() >= trip.check_out:
                issues.append("arrival_on_or_after_hotel_checkout")
            elif trip.check_in and arrival.date() > trip.check_in:
                issues.append("arrival_after_hotel_checkin_date")
    if outbound_arrival and inbound_departure:
        try:
            if inbound_departure <= outbound_arrival:
                issues.append("return_departs_before_outbound_arrival")
        except TypeError:
            unknown.append("inconsistent_leg_timezones")
    return {
        "status": "conflict" if issues else "unverified" if unknown else "consistent",
        "issues": list(dict.fromkeys(issues)), "unverified_candidates": unknown,
    }


ISSUE_LABELS = {
    "hotel_date_mismatch": "酒店日期与请求不一致",
    "transport_departure_date_mismatch": "交通出发日期与请求不一致",
    "arrival_not_after_departure": "到达时间不晚于出发时间",
    "overnight_return_requires_confirmation": "返程次日才到达，需要确认实际返回日期",
    "arrival_on_or_after_hotel_checkout": "去程到达时已到酒店退房日期，住宿安排冲突",
    "arrival_after_hotel_checkin_date": "去程到达晚于计划入住日期，需要确认延迟入住",
    "return_departs_before_outbound_arrival": "返程早于或等于去程到达时间",
}


def _candidate_display(item):
    kind = KIND_LABELS.get(item.get("kind"), "候选")
    label = single_line(item.get("label") or "候选方案")
    return f"{kind}：{label}"


def _candidate_display_map(draft):
    items = [*draft.get("selected", []), *draft.get("alternatives", [])]
    return {
        item.get("id"): _candidate_display(item)
        for item in items
        if isinstance(item, dict) and item.get("id")
    }


def _policy_check_display(result, candidate_labels, draft):
    candidate_id = result.get("candidate_id")
    if candidate_id in candidate_labels:
        return candidate_labels[candidate_id]
    kind = result.get("kind") or draft.get("request", {}).get("kind")
    if not kind and draft.get("request", {}).get("query_scope") == "hotel":
        kind = "hotel"
    return f"{KIND_LABELS.get(kind, '候选')}候选方案"


def render_hotel_detail(item):
    detail = item.get("details", {}).get("hotel_detail", {})
    status = detail.get("status")
    if status != "ok":
        return ["房型详情：" + ("查询失败，保留搜索候选，不代表没有房间。"
                                if status == "error" else "尚未查询，房型与库存待核实。")]
    availability = {"available_snapshot": "供应商返回有库存快照，不保证预订时仍可用",
                    "sold_out": "本次返回的报价库存均为零，不代表其他渠道也无房",
                    "unknown": "库存未知"}
    lines = ["房型详情：" + availability.get(detail.get("availability"), "库存未知") + "。"]
    requested = detail.get("requested", {})
    lines.append(f"查询入住：{requested.get('checkIn', '未知')}；退房：{requested.get('checkOut', '未知')}；"
                 "响应未独立核实入住日期。")
    labels = {"roomNum": "房间数", "adultNum": "成人数", "childNum": "儿童数"}
    lines.append("入住条件：" + "；".join(
        f"{label}：{requested[key]}" if key in requested else f"{label}：供应商默认（非用户已确认）"
        for key, label in labels.items()) + "。")
    for rate in detail.get("rates", []):
        stock = rate.get("remaining_count")
        lines.append(f"- {rate.get('room_name') or '房型未说明'} / {rate.get('rate_name') or '报价方案未说明'}；"
                     f"床型：{rate.get('bed_type') or '未说明'}；"
                     f"报价：{rate.get('amount') if rate.get('amount') is not None else '未知'}元"
                     "（含税；每晚/全程口径待确认）；"
                     f"餐食：{rate.get('meal') or '未说明'}；"
                     f"退改：{rate.get('cancellation') or '未说明'}；"
                     f"返回库存：{stock if stock is not None else '未知'}。")
        if rate.get("truncated_fields"):
            lines.append("该报价的部分文字已截断，不能据此确认完整餐食或退改条件；请到平台查看完整条款。")
    if not detail.get("rates"):
        lines.append("没有返回可解析的房型报价，不据此断言无房。")
    if detail.get("rates_truncated"):
        lines.append("仅展示部分返回报价，不代表全部房型或全网最低价。")
    lines.append("以上仅供参考；请自行到对应平台核实房型、总价及退改条款并预订。")
    return lines


def _amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


def _money(value, currency):
    amount = _amount(value)
    if amount is None:
        return "价格待核实"
    formatted = format(amount, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return f"{formatted} {single_line(currency or '币种待核实')}"


def _policy_summary(draft, candidate_labels):
    checks = draft.get("policy_checks", [])
    main, details, missing, reasons = [], [], [], []
    for result in checks:
        display = _policy_check_display(result, candidate_labels, draft)
        status = result.get("status")
        reason = result.get("reason")
        explanation = policy_reason_label(reason)
        status_label = POLICY_STATUS_LABELS.get(status, "尚未取得有效核验结果")
        entry = f"- {display}：{status_label}；{explanation}。"
        amounts = [f"{label}：{result[key]} {result.get('currency', '')}".rstrip()
                   for key, label in (("cap", "适用限额"), ("amount", "核验金额"), ("excess", "超出金额"))
                   if key in result]
        details.append(entry + (" " + "；".join(amounts) if amounts else ""))
        if status == "within_checked_rules":
            main.append(f"- {display}：在本次已核对的一般标准范围内。" +
                        (" " + "；".join(amounts) if amounts else ""))
        elif status in {"over_standard", "over_cap", "manual_review"}:
            # Actual limits, overages and exception requirements stay outside collapsed details.
            main.append(entry + (" " + "；".join(amounts) if amounts else ""))
        else:
            missing.extend(result.get("missing_fields", []))
            if reason != "required_facts_missing":
                reasons.append(explanation + "。")
    if not checks or any(check.get("status") != "within_checked_rules" for check in checks):
        main.insert(0, "尚未完成个人差旅标准核对，不能确认合规。")
    if any(check.get("status") == "within_checked_rules" for check in checks):
        main.append("以上仅核对所列项目，不代表完整合规；总预算尚未完整核验。")
    if missing:
        main.append("请补充或核实：" + "、".join(dict.fromkeys(
            FIELD_LABELS.get(field, "其他必要事实") for field in missing)) + "。")
    main.extend(dict.fromkeys(reasons))
    return main, details


def _recommendation_profile_lines(draft, request):
    profile = draft.get("recommendation_profile", {})
    if profile.get("status") != "ok":
        return [], []
    raw_sources = profile.get("sources")
    raw_sources = raw_sources if isinstance(raw_sources, list) else []
    sources = {s["source_id"]: s for s in raw_sources
               if isinstance(s, dict) and isinstance(s.get("source_id"), str)}
    bindings = profile.get("standard_citations")
    bindings = bindings if isinstance(bindings, dict) else {}
    groups, evidence_details = {}, []

    def cite(field):
        ids = bindings.get(field, [])
        if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or key not in sources for key in ids):
            return ""
        ids = tuple(dict.fromkeys(ids))
        evidence = [sources[key] for key in ids]
        for item in evidence:
            if (any(not isinstance(item.get(key), str) or not item[key].strip()
                    for key in ("source", "version", "source_sha256", "chunk", "text"))
                    or not isinstance(item.get("document_id"), (int, str))
                    or isinstance(item.get("document_id"), bool) or not item.get("document_id")
                    or item.get("document_id") != profile.get("policy_document_id")
                    or item["version"] != profile.get("policy_version")
                    or item["source_sha256"] != profile.get("policy_source_sha256")):
                return ""
        if ids not in groups:
            name = f"来源{len(groups) + 1}"
            groups[ids] = name
            excerpts = []
            for item in evidence:
                label = " / ".join(str(item[key]) for key in
                    ("source", "version", "clause", "section", "sheet", "cell_range") if item.get(key))
                kind = ("配置摘录（项目已审核配置，非制度原文）"
                        if item.get("source_type") == "reviewed_rule" else "原文摘录")
                excerpts.append(label + "\n" + kind + "：\n" + item["text"])
            evidence_details.append(detail_block("政策依据 · " + name, "\n\n".join(excerpts)))
        return f" [{groups[ids]}]"

    fragment_mode = profile.get("citation_mode", "fragments") != "grouped"
    assumptions = []
    if fragment_mode:
        from .recommendation_evidence import fragment_references
        cite, evidence_details, assumptions = fragment_references(profile, request)

    lines = ["\n### 差旅标准参考"]
    if request.get("query_scope") != "hotel":
        lines.append("交通类别：" + single_line(profile.get("transport_standard") or "待核实") + cite("transport_standard"))
        labels = {"high_speed_train": "高铁", "regular_train": "普通列车", "flight": "飞机"}
        allowed = profile.get("allowed_transport", {})
        requested_mode = request.get("transport", "compare")
        for mode, label in labels.items():
            if requested_mode == "train" and mode == "flight":
                continue
            if requested_mode == "flight" and mode != "flight":
                continue
            if request.get("train_service") == "high_speed" and mode == "regular_train":
                continue
            classes = allowed.get(mode) if isinstance(allowed, dict) else None
            if classes:
                lines.append(f"{label}可乘：" + "、".join(single_line(item) for item in classes) + cite("allowed_transport"))
    if request.get("query_scope") != "transport":
        lines.append("住宿档次：" + single_line(profile.get("lodging_standard") or "待核实") + cite("lodging_standard"))
        if profile.get("lodging_cap") is not None:
            amount = _money(profile.get("lodging_cap"), profile.get("lodging_currency"))
            if profile.get("lodging_currency") == "CNY":
                amount = amount.replace(" CNY", "元")
            unit = {"person_day": "人·天", "room_night": "间·晚"}.get(
                profile.get("lodging_unit"), profile.get("lodging_unit") or "人·天")
            lines.append("住宿参考上限：" + amount + "/" + unit + cite("lodging_cap"))
        else:
            lines.append("住宿参考上限：待核实")
    if fragment_mode:
        applicability = cite("lodging_applicability")
        if applicability:
            evidence_details.insert(0, "学生住宿标准的适用范围见条款" + applicability + "。")
        if assumptions:
            evidence_details.append(detail_block("推荐说明", "\n".join(assumptions)))
    return lines, evidence_details


def _recommendation_check_map(draft):
    return {
        item.get("candidate_id"): item
        for item in draft.get("recommendation_checks", [])
        if isinstance(item, dict) and item.get("candidate_id")
    }


def _recommendation_notes(draft, request, notes, explanations):
    profile = draft.get("recommendation_profile", {})
    if profile.get("status") != "ok":
        return
    checks = _recommendation_check_map(draft)
    all_items = [*draft.get("selected", []), *draft.get("alternatives", [])]
    groups = ("hotel",) if request.get("query_scope") == "hotel" else (
        "outbound", "inbound") if request.get("query_scope") == "transport" else ("outbound", "inbound", "hotel")
    for group in groups:
        candidates = [item for item in all_items
                      if ("hotel" if item.get("kind") == "hotel"
                          else "inbound" if item.get("details", {}).get("inbound") else "outbound") == group]
        if group == "hotel":
            candidates = [item for item in candidates
                          if item.get("details", {}).get("hotel_detail", {}).get("availability")
                          != "sold_out"]
        if not candidates:
            continue
        statuses = {checks.get(item.get("id"), {}).get("status") for item in candidates}
        if "within_standard" in statuses:
            continue
        standard = (profile.get("lodging_standard") if group == "hotel"
                    else profile.get("transport_standard")) or "当前标准"
        if "over_standard" in statuses and not (statuses & {"unknown", "unverified"}):
            notes.append(f"当前检索结果中暂未找到符合{standard}的{('酒店' if group == 'hotel' else '交通')}，"
                         "已保留超标准结果作为备选。")
        elif statuses & {"unknown", "unverified"}:
            explanations.append(f"当前候选暂不能确认是否符合{standard}，请核实报价或席别后再决定。")


def _room_reference(detail):
    rates = [rate for rate in detail.get("rates", [])
             if isinstance(rate, dict) and type(rate.get("remaining_count")) is int
             and rate["remaining_count"] > 0 and _amount(rate.get("amount")) is not None]
    if not rates:
        return []
    # Surface one real returned offer; do not infer nightly units or new inventory.
    rate = min(rates, key=lambda item: _amount(item["amount"]))
    lines = ["房型参考：" + single_line(rate.get("room_name") or "房型未说明")]
    if rate.get("bed_type"):
        lines.append("床型：" + single_line(rate["bed_type"]))
    if rate.get("meal"):
        lines.append("早餐/餐食：" + single_line(rate["meal"]))
    lines.append("房型参考报价：" + _money(rate["amount"], rate.get("currency") or "CNY"))
    return lines


def _query_condition_text(draft):
    context = render_fact_context(draft.get("fact_context", {}), compact=True)
    defaults = draft.get("query_defaults", {})
    labels = {"mainland_business_trip": "是否大陆因公出差", "hotel_rooms": "房间数",
              "hotel_adults": "成人数", "hotel_child_ages": "儿童年龄", "room_occupants": "入住人数"}
    extra = []
    for key, label in labels.items():
        if key not in defaults:
            continue
        value = defaults[key]
        if key == "hotel_child_ages":
            label, value = "儿童数", len(value)
        elif isinstance(value, bool):
            value = "是" if value else "否"
        extra.append(f"- {label}：{single_line(value)}（默认）")
    if not extra:
        return context
    # Keep defaulted facts separate from the user's evidence-backed facts.
    if context:
        lines = context.splitlines()
        return "\n".join([*lines[:-1], "查询默认条件：", *extra, lines[-1]])
    return "\n".join(["本次查询默认条件：", *extra, "如需修改行程信息，直接告诉我即可。"])


def render_draft(draft):
    if not draft:
        return "未取得有效行程草案，不能确认规划完成。"
    candidate_labels = _candidate_display_map(draft)
    recommendation_checks = _recommendation_check_map(draft)
    request = draft.get("request", {})
    title = {"hotel": "住宿参考", "transport": "交通参考"}.get(request.get("query_scope"), "差旅参考方案")
    if request.get("origin") and request.get("destination"):
        arrow = "⇄" if request.get("journey") == "round_trip" else "→"
        title = f"{single_line(request['origin'])} {arrow} {single_line(request['destination'])}"
    lines, notes, details, explanations = ["### " + title], [], [], []
    if request.get("departure_date"):
        lines.append(f"{request['departure_date']}" +
                     (f" 至 {request['return_date']}" if request.get("return_date") else " 出发"))
    if draft.get("selection_status") == "context_budget_exceeded":
        notes.append("本次候选较多，超过处理预算；已查到的候选仍保留，请缩小查询范围后重试。")
    elif draft.get("selection_status") in {"error", "invalid_selection"}:
        notes.append("候选选择未完成，以下已查到的结果仅供参考，尚未选定行程。")
    if draft.get("selection_status") == "selected_with_hotel_fallback":
        explanations.append("模型未完成酒店选择，系统已根据库存和候选排序补选一间酒店；请继续核实房型、价格和退改条款。")
    for assumption in draft.get("assumptions", []):
        explanations.append("默认处理：" + assumption + "；如不适用请直接纠正。")
    if draft.get("hotel_inventory_status") == "sold_out":
        notes.append("已检索到周边酒店，但当前返回的酒店均暂无可用库存。")
    for rejected in draft.get("rejected_selections", []):
        for issue in rejected["timeline_check"]["issues"]:
            notes.append("已排除冲突组合：" + ISSUE_LABELS.get(issue, "存在尚未解释的时间冲突，请核实日期"))
    for query in draft.get("query_status", []):
        if query.get("status") in {"error", "unavailable", "empty"} or not query.get("count"):
            label = KIND_LABELS.get(query["kind"], "候选")
            prefix = f"{'返程' if query.get('inbound') else ''}{label}"
            if query.get("status") == "empty":
                notes.append(prefix + "查询成功，当前条件下未找到候选。")
            elif query.get("status") in {"error", "unavailable"}:
                notes.append(prefix + "查询失败或服务不可用，未取得可用候选；不代表没有供应，请稍后重试。")
            else:
                notes.append(prefix + "返回结果中没有符合本次条件的可用候选。")
    selected = draft.get("selected", [])
    items = selected or draft.get("alternatives", [])
    _recommendation_notes(draft, request, notes, explanations)
    if not selected and items:
        lines.append("已查到以下待选候选，尚未完成逐项政策及通勤核验。")
    transport = [item for item in items if item.get("kind") != "hotel"]
    if transport:
        lines.extend(["\n### 交通参考", table_row(["行程", "车次 / 航班", "出发", "到达", "参考票价"]),
                      table_row(["---"] * 5)])
        for item in transport:
            quote = item.get("quote", {})
            lines.append(table_row([
                "返程" if item.get("details", {}).get("inbound") else "去程",
                _candidate_display(item), item.get("departure") or "待核实", item.get("arrival") or "待核实",
                _money(quote.get("amount"), quote.get("currency")) +
                f" / {QUOTE_LABELS.get(quote.get('basis'), '计价口径待核实')}"]))
        currencies = {item.get("quote", {}).get("currency") for item in transport}
        prices = [_amount(item.get("quote", {}).get("amount")) for item in transport]
        if (selected and len(transport) > 1 and len(currencies) == 1 and all(currencies)
                and all(price is not None for price in prices)
                and all(item.get("quote", {}).get("basis") == "ticket" for item in transport)):
            lines.append("\n所列交通票价合计约 " + _money(sum(prices), next(iter(currencies))) +
                         "（各一张票，不含住宿及其他费用）。")
    hotels = [item for item in items if item.get("kind") == "hotel"]
    if hotels:
        lines.append("\n### 住宿参考")
        if request.get("check_in") and request.get("check_out"):
            stay = f"入住：{request['check_in']}；退房：{request['check_out']}"
            try:
                nights = (date.fromisoformat(str(request["check_out"])) -
                          date.fromisoformat(str(request["check_in"]))).days
                if nights > 0:
                    stay += f"；共{nights}晚"
            except ValueError:
                pass
            lines.append(stay + "。")
        for item in hotels:
            lines.append("\n" + _candidate_display(item))
            if item.get("address"):
                lines.append("地址：" + single_line(item["address"]))
            quote = item.get("quote", {})
            lines.append("搜索起价：" + _money(quote.get("amount"), quote.get("currency")))
            recommendation = recommendation_checks.get(item.get("id"), {})
            recommendation_status = recommendation.get("status")
            if recommendation_status == "within_standard":
                lines.append("推荐筛选：按当前搜索起价估算，不高于住宿参考上限。")
            elif recommendation_status == "over_standard":
                lines.append("推荐筛选：按当前搜索起价估算，高于住宿参考上限，仅作为超标准备选。")
            elif recommendation_status in {"unknown", "unverified"}:
                explanations.append("酒店报价与适用标准尚未完整匹配。")
            quote_notes = [f"计价口径：{QUOTE_LABELS.get(quote.get('basis'), '待核实')}。"]
            if quote.get("basis") == "night" and item.get("date_verified"):
                quote_notes.append("此处仅列参考房价，未汇总全程住宿费用。")
            else:
                quote_notes.append("当前报价按供应商含税价格展示；搜索起价的每晚/全程口径仍需结合房型详情确认，暂不计入总费用。")
            detail = item.get("details", {}).get("hotel_detail", {})
            if detail.get("status") == "ok":
                lines.extend(_room_reference(detail))
            if detail.get("availability") == "sold_out":
                lines.append("本次返回的报价库存均为零，请更换酒店或到平台核实其他房型。")
            elif detail.get("status") != "ok":
                lines.append("房型查询失败，暂不能确认有房。" if detail.get("status") == "error"
                             else "尚未取得房型详情，暂不能确认有房。")
            else:
                quote_notes.append("已返回部分房型信息，入住日期及余量仍需平台确认。")
            if detail.get("rates"):
                quote_notes.append("取消或提前离店可能收费，预订前请核实房型、早餐及退改条款。")
            details.append(detail_block("房型与退改详情 · " + single_line(item.get("label", "酒店")),
                                        "\n".join([*quote_notes, *render_hotel_detail(item)])))
    timeline = draft.get("timeline_check", {})
    for issue in timeline.get("issues", []):
        notes.append("时间冲突待确认：" + ISSUE_LABELS.get(issue, "存在尚未解释的时间冲突，请核实日期"))
    if timeline.get("unverified_candidates"):
        explanations.append("部分候选的完整出发/到达时间未核实。")
    if draft.get("missing_components"):
        labels = {"outbound": "去程交通", "inbound": "返程交通", "hotel": "酒店"}
        missing_labels = [labels.get(key, "其他项目") for key in draft["missing_components"]]
        if draft.get("hotel_inventory_status") == "sold_out":
            missing_labels = [label for label in missing_labels if label != "酒店"]
        if missing_labels:
            notes.append("方案仍缺少：" + "、".join(missing_labels))
    for ident, result in draft.get("commute_checks", {}).items():
        display = candidate_labels.get(ident, "该候选方案")
        if result.get("status") == "ok":
            lines.append(f"通勤估算（{display}）：{result.get('distance_meters', '未知')}米，"
                         f"{result.get('duration_seconds', '未知')}秒；仅当前估算，不保证未来实际耗时。")
        else:
            lines.append(f"通勤（{display}）：未取得有效估算。")
    standard_lines, standard_evidence = _recommendation_profile_lines(draft, request)
    lines.extend(standard_lines)
    policy, policy_details = _policy_summary(draft, candidate_labels)
    for result, explanation in zip(draft.get("policy_checks", []), policy_details):
        if result.get("status") in {"over_standard", "over_cap", "manual_review"}:
            notes.append(explanation)
    if notes:
        lines.extend(["\n### 行程提醒", *dict.fromkeys(notes)])
    lines.append("\n以上仅供规划参考，价格与余量以预订平台为准，请自行核实后预订。")
    if policy_details:
        details.append(detail_block("查看逐项政策核验", "\n".join(policy_details)))
    backups = [item for item in draft.get("alternatives", [])
               if recommendation_checks.get(item.get("id"), {}).get("status") == "over_standard"]
    if backups:
        details.append(detail_block(
            "超标准备选",
            "\n".join(
                f"- {_candidate_display(item)}；参考价格：{_money(item.get('quote', {}).get('amount'), item.get('quote', {}).get('currency'))}"
                for item in backups[:5]
            ) + "\n以上候选仅作备选，请先核实是否存在适用的例外或审批。",
        ))
    unmapped = [item for item in draft.get("alternatives", [])
                if recommendation_checks.get(item.get("id"), {}).get("reason")
                == "transport_class_mapping_required"]
    if unmapped:
        details.append(detail_block(
            "席别标准待核实的备选",
            "\n".join(f"- {_candidate_display(item)}" for item in unmapped) +
            "\n这些席别尚未与当前政策名称明确匹配，暂不判定为超标准。"))
    details.append(detail_block("核验与补充说明", "\n".join([*explanations, *policy])))
    context = _query_condition_text(draft)
    if context:
        details.append(detail_block("查看本次查询条件", context))
    details.extend(standard_evidence)
    lines.append("\n### 补充说明")
    lines.extend("\n" + block for block in details)
    return "\n".join(lines)
