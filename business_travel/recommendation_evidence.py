"""Project recommendation evidence without exposing the complete audit context."""

import hashlib
import json
import re

from .presentation import detail_block, single_line

MODE_LABELS = {"high_speed_train": "高铁", "regular_train": "普通列车", "flight": "飞机"}


def _modes(request):
    mode = request.get("transport", "compare")
    return [key for key in MODE_LABELS
            if not (mode == "flight" and key != "flight")
            and not (mode == "train" and key == "flight")
            and not (request.get("train_service") == "high_speed" and key == "regular_train")]


def attach_fragments(profile, rows, rules, raw_rules, trip):
    """Keep audit sources intact; bind display fragments to their parent sources."""
    fragments, bindings = [], {}
    parents = {s["source_id"]: s for s in profile["sources"]}
    sources = {s["chunk"]: s for s in profile["sources"] if s.get("source_type") != "reviewed_rule"}
    category = profile.get("transport_standard")
    lodging = profile.get("lodging_standard")

    def add(source_id, kind, fields, **extra):
        ident = f"F{len(fragments) + 1}"
        fragments.append({"fragment_id": ident, "source_id": source_id, "kind": kind,
                          "fields": fields, "source_text_sha256": hashlib.sha256(
                              parents[source_id]["text"].encode("utf-8")).hexdigest(), **extra})
        for field in fields:
            bindings.setdefault(field, []).append(ident)
        return ident

    def original(row, fields, values, quotes=None):
        source = sources.get(row["chunk_id"])
        if source:
            quotes = quotes or [row["content"]]
            add(source["source_id"], "original", fields, values=values,
                quotes=quotes, quotes_sha256=hashlib.sha256(
                    json.dumps(quotes, ensure_ascii=False).encode("utf-8")).hexdigest())

    if trip.query_scope != "hotel" and category:
        # Match a structured category cell, never a substring in arbitrary prose.
        matching = []
        for row in rows:
            table = row.get("table_data") or {}
            headers, cells = table.get("headers", []), table.get("cells", [])
            if (row.get("clause") in rules.source_clauses["transport"] and "标准" in headers
                    and len(headers) == len(cells) and cells[headers.index("标准")] == category):
                matching.append(row)
        if len(matching) == 1:
            row = matching[0]
            table = row["table_data"]
            quotes = []
            modes = _modes(trip.model_dump())
            for header, cell in zip(table["headers"], table["cells"]):
                normalized = "".join(header.split())
                if (header in {"标准", "对应人员"}
                        or (any(m != "flight" for m in modes) and "火车" in normalized)
                        or ("flight" in modes and "飞机" in normalized)):
                    quote = f"{header}：{cell}"
                    if quote in row["content"]:
                        quotes.append(quote)
            # Preserve all personnel qualifiers when selecting table columns.
            if len(quotes) < 2 or not any(q.startswith("对应人员：") for q in quotes):
                quotes = [row["content"]]
            original(row, ["transport_standard", "allowed_transport"],
                     {"transport_standard": category}, quotes)
        allowed = {mode: profile["allowed_transport"][mode] for mode in _modes(trip.model_dump())
                   if profile["allowed_transport"].get(mode)}
        if allowed:
            add("P1", "transport_rule", ["allowed_transport"],
                values={"transport_standard": category}, allowed=allowed)

    if (trip.query_scope != "hotel" and category
            and trip.technical_grade is not None and trip.management_grade is not None):
        for row in rows:
            if (row.get("clause") in rules.source_clauses["transport"]
                    and not (row.get("table_data") or {}).get("cells")):
                quotes = [part.strip() for part in re.split(r"(?=[（(][一二三四五六七八九十]+[）)])", row["content"])
                          if "就高" in part]
                if quotes:
                    original(row, ["transport_standard"], {"transport_standard": category}, quotes)

    if trip.personnel_type == "student":
        # The activity clause explains applicability. It does not assign a grade.
        fields = []
        if trip.query_scope != "hotel":
            fields.append("transport_standard")
        if trip.query_scope != "transport":
            fields.append("lodging_applicability")
        for row in rows:
            if (not fields or row.get("clause") not in rules.source_clauses["transport"]
                    or (row.get("table_data") or {}).get("cells")
                    or "学生" not in row["content"]):
                continue
            original(row, fields, {"transport_standard": category})
            source = sources.get(row["chunk_id"])
            if not source:
                continue
            citations = profile.setdefault("standard_citations", {})
            if "lodging_applicability" in fields:
                bound = citations.setdefault("lodging_applicability", [])
                if source["source_id"] not in bound:
                    bound.append(source["source_id"])
            if "transport_standard" in fields and not category:
                bound = citations.setdefault("transport_standard", [])
                if source["source_id"] not in bound:
                    bound.append(source["source_id"])

    if trip.query_scope != "transport" and lodging:
        cap_ids = set(profile["standard_citations"].get("lodging_cap", []))
        for row in rows:
            source = sources.get(row["chunk_id"])
            if source and source["source_id"] in cap_ids:
                original(row, ["lodging_cap"], {key: profile.get(key) for key in
                    ("lodging_standard", "lodging_cap", "lodging_currency", "lodging_unit", "lodging_date")})
        if len(bindings.get("lodging_cap", [])) != len(cap_ids):
            bindings.pop("lodging_cap", None)
    context_fields = ("query_scope", "transport", "train_service", "academic_title", "technical_grade",
                      "management_grade", "personnel_type", "student_activity_eligible", "transport_standard",
                      "lodging_standard", "destination", "destination_province", "check_in", "check_out")
    request = trip.model_dump(mode="json")
    profile.update(citation_fragments=fragments, fragment_citations=bindings,
                   citation_context={key: request.get(key) for key in context_fields})


def _valid_source(source, profile):
    return (isinstance(source, dict)
            and all(isinstance(source.get(key), str) and source[key].strip()
                    for key in ("source", "version", "source_sha256", "chunk", "text"))
            and isinstance(source.get("document_id"), (int, str))
            and not isinstance(source.get("document_id"), bool) and bool(source.get("document_id"))
            and source["document_id"] == profile.get("policy_document_id")
            and source["version"] == profile.get("policy_version")
            and source["source_sha256"] == profile.get("policy_source_sha256"))


def _project_fragment(fragment, source, profile, request):
    values = fragment.get("values")
    if not isinstance(values, dict) or not values or any(profile.get(k) != v for k, v in values.items()):
        return None
    label = " / ".join(str(source[key]) for key in
        ("source", "version", "clause", "section", "sheet", "cell_range") if source.get(key))
    if type(source.get("table_row")) is int and source["table_row"] > 0:
        label += f" / 表格第{source['table_row']}行"
    kind = fragment.get("kind")
    if hashlib.sha256(source["text"].encode("utf-8")).hexdigest() != fragment.get("source_text_sha256"):
        return None
    if kind == "original":
        quotes = fragment.get("quotes")
        if (source.get("source_type") not in (None, "original") or not isinstance(quotes, list) or not quotes
                or any(not isinstance(q, str) or not q.strip() or q not in source["text"] for q in quotes)):
            return None
        if hashlib.sha256(json.dumps(quotes, ensure_ascii=False).encode("utf-8")).hexdigest() != fragment.get("quotes_sha256"):
            return None
        quotes = tuple(dict.fromkeys(quotes))
        if "lodging_cap" in values and quotes != (source["text"],):
            return None
        identity = (source.get("source"), source.get("file_sha256", source["source_sha256"]), source["document_id"],
                    source["version"], source["chunk"], source.get("sheet"), source.get("cell_range"), quotes)
        excerpt_label = "原文摘录（相关表格单元格）：" if len(quotes) > 1 else "原文摘录："
        content = label + "\n" + excerpt_label + "\n" + "；".join(quotes)
        if request.get("personnel_type") == "student" and "学生" in source["text"]:
            eligible = request.get("student_activity_eligible")
            if eligible is False:
                content += "\n适用说明：本次已明确不属于上述活动，不能据此确认个人适用性。"
            elif eligible is not True:
                content += "\n适用说明：本次活动类型尚未确认，适用范围以上述条款为准。"
                if profile.get("transport_standard"):
                    content += "暂按" + single_line(profile["transport_standard"]) + "提供推荐参考。"
        return identity, content
    if source.get("source_type") != "reviewed_rule":
        return None
    try:
        config = json.loads(source["text"])["采用的规则配置"]
        if not isinstance(config, dict):
            return None
        category = profile.get("transport_standard")
        if kind == "transport_rule":
            allowed = fragment.get("allowed")
            expected = {mode: profile.get("allowed_transport", {}).get(mode) for mode in _modes(request)
                        if profile.get("allowed_transport", {}).get(mode)}
            if not isinstance(allowed, dict) or not allowed or allowed != expected:
                return None
            configured = config["transport_classes"][category]
            if any(configured.get(mode) != seats for mode, seats in allowed.items()):
                return None
            text = ["项目席别口径（非制度原文）：", f"本次按{single_line(category)}推荐。"]
            text += [f"{MODE_LABELS[mode]}：" + "、".join(single_line(s) for s in seats)
                     for mode, seats in allowed.items()]
            text.append("以上为项目采用的可选席别范围，包含较低席别，不是制度逐字表述。")
        else:
            return None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    identity = (source["document_id"], source["version"], source.get("file_sha256"), kind,
                json.dumps(text, ensure_ascii=False))
    return identity, f"项目推荐口径 / {source['version']}\n" + "\n".join(text)


def fragment_references(profile, request):
    """Cite original clauses; keep project mappings as unnumbered assumptions."""
    raw_sources = profile.get("sources")
    raw_fragments = profile.get("citation_fragments")
    bindings = profile.get("fragment_citations")
    sources, fragments = {}, {}
    for items, key, target in ((raw_sources, "source_id", sources),
                               (raw_fragments, "fragment_id", fragments)):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get(key), str):
                ident = item[key]
                # Ambiguous IDs must not silently select one of two conflicting records.
                target[ident] = item if ident not in target else None
    refs, details, assumptions = {}, [], []

    def cite(field):
        context = profile.get("citation_context")
        if not isinstance(context, dict) or not context or any(request.get(k) != v for k, v in context.items()):
            return ""
        ids = bindings.get(field) if isinstance(bindings, dict) else None
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            return ""
        audit_bindings = profile.get("standard_citations")
        audit_ids = audit_bindings.get(field) if isinstance(audit_bindings, dict) else None
        if (not isinstance(audit_ids, list) or not audit_ids
                or any(not isinstance(i, str) for i in audit_ids)):
            return ""
        projected = []
        for ident in dict.fromkeys(ids):
            fragment = fragments.get(ident)
            parent = fragment.get("source_id") if isinstance(fragment, dict) else None
            fields = fragment.get("fields") if isinstance(fragment, dict) else None
            if (not isinstance(parent, str) or parent not in audit_ids
                    or not isinstance(fields, list) or field not in fields):
                return ""
            source = sources.get(parent)
            if not _valid_source(source, profile):
                return ""
            if any(source.get(key) is not None and not isinstance(source[key], str)
                   for key in ("file_sha256", "sheet", "cell_range")):
                return ""
            item = _project_fragment(fragment, source, profile, request)
            if item is None:
                return ""
            projected.append((fragment, item))
        names = []
        for fragment, (identity, content) in projected:
            if fragment["kind"] != "original":
                continue
            if field == "lodging_standard" and "lodging_standard" not in fragment["values"]:
                continue
            if identity not in refs:
                refs[identity] = f"来源{len(refs) + 1}"
                details.append(detail_block("政策依据 · " + refs[identity], content))
            if refs[identity] not in names:
                names.append(refs[identity])
        return " " + " ".join(f"[{name}]" for name in names) if names else ""

    return cite, details, assumptions
