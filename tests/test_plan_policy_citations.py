"""Recommendation policy references must reach the actual itinerary renderer."""

import copy
import json
import unittest
from dataclasses import replace
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from business_travel.contracts import parse_query_request
from business_travel.plan_checks import render_draft
from business_travel.policy_package import prepare_package
from business_travel.university_policy import load_reviewed_rules, planning_standards_for
from business_travel.reply_renderer import finalize_business_reply
from test_plan_presentation import draft, main_text
from test_policy_rag import database_doc, store_with_db


def recommendation_fixture(scope="combined", *, omit_sources=False, citation_mode="fragments",
                           trip_changes=None, legacy_rules=False, row_transform=None,
                           lodging_standard=None):
    bundle = prepare_package(Path(__file__).resolve().parents[1] /
                             "policies/hfut-2026/manifest.json", company_id="citation-test")
    clauses = set(bundle.rules["source_clauses"]["scope"] + bundle.rules["source_clauses"]["transport"])
    rows = [{"chunk_id": c.id, "clause": c.clause, "section": c.section,
             "content": c.text, "table_data": c.table, "related_ids": c.related_ids}
            for c in bundle.chunks if c.clause in clauses or c.id == "lodging-r9"]
    raw_rules = copy.deepcopy(bundle.rules)
    if legacy_rules:
        raw_rules.pop("lodging_categories", None)
    if row_transform:
        rows = row_transform(rows)
    changes = dict(trip_changes or {})
    if lodging_standard is not None:
        changes.setdefault("lodging_standard", lodging_standard)
    trip = parse_query_request({"query_scope": scope, "origin": "南京", "destination": "北京",
                                "departure_date": "2026-10-19", "check_in": "2026-10-19",
                                "check_out": "2026-10-20", "academic_title": "院士",
                                "journey": "round_trip", "return_date": "2026-10-20", **changes})
    _, prescribed = planning_standards_for(trip, load_reviewed_rules(raw_rules))
    results = [[database_doc(policy_profile="university")], {"rules": raw_rules}]
    if scope != "transport" and trip.destination and prescribed:
        results.append([{"data": r} for r in bundle.lodging_rows])
    results.append([] if omit_sources else rows)
    store, conn, _ = store_with_db(results)
    store.settings = replace(store.settings, policy_citation_mode=citation_mode)
    profile = store.recommendation_profile(trip)
    data = draft()
    data["request"] = trip.model_dump(mode="json")
    data["recommendation_profile"] = profile
    for item in data["selected"]:
        for key in ("departure", "arrival"):
            if key in item:
                item[key] = item[key].replace("2099-10-12", "2026-10-19").replace("2099-10-14", "2026-10-20")
        if item.get("kind") == "hotel":
            item["address"] = "北京示例路（验收数据）"
            item["details"]["hotel_detail"]["requested"] = {"checkIn": "2026-10-19", "checkOut": "2026-10-20"}
    if scope != "combined":
        data["selected"] = [item for item in data["selected"] if (item["kind"] == "hotel") == (scope == "hotel")]
    return data, conn


class PlanPolicyCitationTests(unittest.TestCase):
    def test_default_policy_evidence_contains_only_original_sources(self):
        data, _ = recommendation_fixture(lodging_standard="部级")
        text = render_draft(data)
        self.assertNotIn("项目推荐口径 /", text)
        self.assertNotIn("非制度原文", text)
        self.assertNotIn("项目席别口径", text)
        self.assertNotIn("住宿档次项目口径", text)
        self.assertEqual(text.count(":::details 政策依据"), 2)
        self.assertIn("住宿参考上限：1100元/人·天 [来源2]", text)
        self.assertIn("第六条", text)
        self.assertIn("Sheet1 / B9:K9", text)

    def test_transport_identity_does_not_supply_lodging_grade(self):
        data, _ = recommendation_fixture(legacy_rules=True)
        text = render_draft(data)
        profile = data["recommendation_profile"]
        self.assertEqual(profile["transport_standard"], "一类")
        self.assertIsNone(profile["lodging_standard"])
        self.assertNotIn("lodging_cap", profile)
        line = next(line for line in main_text(text).splitlines() if line.startswith("住宿档次"))
        self.assertIn("待核实", line)
        self.assertNotIn("部级", line)
        self.assertIn("住宿参考上限：待核实", text)

    def test_non_original_source_cannot_be_reclassified_as_an_original_fragment(self):
        for source_type in ("reviewed_rule", "project_default", "project_rule"):
            with self.subTest(source_type=source_type):
                data, _ = recommendation_fixture(lodging_standard="部级")
                profile = data["recommendation_profile"]
                cap_id = profile["standard_citations"]["lodging_cap"][0]
                source = next(s for s in profile["sources"] if s["source_id"] == cap_id)
                source["source_type"] = source_type
                text = render_draft(data)
                cap = next(line for line in main_text(text).splitlines() if line.startswith("住宿参考上限"))
                self.assertNotIn("[来源", cap)
                self.assertNotIn("Sheet1 / B9:K9", text)

    def test_student_activity_clause_is_not_cited_as_a_lodging_grade_mapping(self):
        data, _ = recommendation_fixture("hotel", trip_changes={"academic_title": None,
            "personnel_type": "student"})
        text = render_draft(data)
        lodging = next(line for line in main_text(text).splitlines() if line.startswith("住宿档次"))
        self.assertNotIn("[来源", lodging)
        self.assertIn("学生住宿标准的适用范围见条款", text)
        self.assertIn("第三十五条", text)
        self.assertIn("教学科研活动、竞赛、社会实践及调研活动", text)

    def test_configuration_and_assigned_standards_remain_available_in_backend(self):
        data, _ = recommendation_fixture()
        before = copy.deepcopy(data)
        text = render_draft(data)
        self.assertEqual(data, before)
        profile = data["recommendation_profile"]
        self.assertTrue(any(s.get("source_type") == "reviewed_rule" for s in profile["sources"]))
        self.assertFalse(any(f.get("kind") == "lodging_rule" for f in profile["citation_fragments"]))
        self.assertEqual(profile["transport_standard"], "一类")
        self.assertIsNone(profile["lodging_standard"])
        self.assertEqual(profile["allowed_transport"]["high_speed_train"], ["商务座", "一等座", "二等座"])
        self.assertNotIn("项目推荐口径", text)
        self.assertNotIn("一类对应", text)

    def test_default_display_deduplicates_shared_excerpts_and_hides_internal_json(self):
        data, _ = recommendation_fixture()
        text = render_draft(data)
        self.assertNotIn('"title_categories"', text)
        self.assertNotIn('"transport_classes"', text)
        self.assertEqual(text.count("标准：一类；对应人员："), 1)
        self.assertNotIn("标准：二类；对应人员：", text)
        self.assertNotIn("标准：三类；对应人员：", text)
        self.assertNotIn("第三十五条", text)
        self.assertNotIn("第二条 /", text)

    def test_shared_transport_reference_reuses_a_single_original_fragment(self):
        data, _ = recommendation_fixture()
        text = render_draft(data)
        lines = main_text(text).splitlines()
        category = next(line for line in lines if line.startswith("交通类别："))
        train = next(line for line in lines if line.startswith("高铁可乘："))
        self.assertIn("[来源1]", category)
        self.assertIn("[来源1]", train)
        self.assertEqual(text.count(":::details 政策依据 · 来源1\n"), 1)
        self.assertIn("50岁及以上，且国际行程达到5小时及以上", text)

    def test_final_business_reply_keeps_itinerary_evidence(self):
        data, _ = recommendation_fixture(lodging_standard="部级")
        messages = [HumanMessage("我是院士，查询南京北京机酒"),
                    ToolMessage(name="plan_trip", tool_call_id="fixture",
                                content=json.dumps({"draft": data}, ensure_ascii=False))]
        reply = finalize_business_reply(messages, AIMessage(content="ignored model summary"))
        self.assertIn("住宿参考上限：1100元/人·天 [来源2]", reply.content)
        self.assertIn(":::details 政策依据 · 来源2", reply.content)
        self.assertNotIn("ignored model summary", reply.content)

    def test_real_profile_carries_scoped_sources_and_exact_lodging_row(self):
        data, conn = recommendation_fixture(lodging_standard="部级")
        profile = data["recommendation_profile"]
        self.assertEqual(profile["status"], "ok")
        self.assertEqual(profile["lodging_cap"], "1100.0")
        sources = {s["source_id"]: s for s in profile["sources"]}
        self.assertTrue(profile["standard_citations"]["allowed_transport"])
        cap_sources = [sources[key] for key in profile["standard_citations"]["lodging_cap"]]
        self.assertEqual([s["chunk"] for s in cap_sources], ["lodging-r9"])
        self.assertEqual(cap_sources[0]["cell_range"], "B9:K9")
        self.assertEqual({s["version"] for s in sources.values()}, {profile["policy_version"]})
        self.assertEqual({s["document_id"] for s in sources.values()}, {1})
        queries = [call.args for call in conn.execute.call_args_list if "FROM policy_chunks" in call.args[0]]
        self.assertTrue(queries)
        self.assertTrue(all(args[1][0] == 1 for args in queries))

    def test_itinerary_standard_lines_have_clickable_reference_targets(self):
        data, _ = recommendation_fixture(lodging_standard="部级")
        before = copy.deepcopy(data)
        text = render_draft(data)
        main = main_text(text)
        for prefix in ("交通类别：一类", "高铁可乘：", "住宿参考上限：1100元/人·天"):
            line = next(line for line in main.splitlines() if line.startswith(prefix))
            self.assertRegex(line, r"\[来源\d+\]")
        self.assertIn(":::details 政策依据 · 来源1", text)
        self.assertIn("Sheet1 / B9:K9", text)
        self.assertIn("原文摘录", text)
        lodging = next(line for line in main.splitlines() if line.startswith("住宿档次：部级"))
        self.assertNotIn("[来源", lodging)
        self.assertNotIn("非制度原文", text)
        self.assertLess(text.index("### 补充说明"), text.index(":::details 政策依据"))
        self.assertEqual(data, before)

    def test_transport_only_does_not_expose_hotel_standard_or_its_reference(self):
        data, _ = recommendation_fixture("transport")
        text = render_draft(data)
        self.assertIn("高铁可乘", main_text(text))
        self.assertIn("[来源1]", main_text(text))
        self.assertNotIn("住宿档次", main_text(text))
        self.assertNotIn("B9:K9", text)

    def test_missing_or_mixed_version_sources_never_create_fake_original_references(self):
        data, _ = recommendation_fixture(lodging_standard="部级")
        cap_id = data["recommendation_profile"]["standard_citations"]["lodging_cap"][0]
        for mutation in ("missing", "different_version", "forged_quote"):
            with self.subTest(mutation=mutation):
                item = copy.deepcopy(data)
                profile = item["recommendation_profile"]
                if mutation == "missing":
                    profile["sources"] = [s for s in profile["sources"] if s["source_id"] != cap_id]
                else:
                    source = next(s for s in profile["sources"] if s["source_id"] == cap_id)
                    source["version" if mutation == "different_version" else "text"] = ""
                line = next(line for line in main_text(render_draft(item)).splitlines()
                            if line.startswith("住宿参考上限"))
                self.assertNotIn("[来源", line)

    def test_legacy_result_without_sources_stays_readable_without_fake_references(self):
        data = draft()
        data["recommendation_profile"] = {"status": "ok", "transport_standard": "一类",
            "lodging_standard": "部级", "lodging_cap": "1100", "lodging_currency": "CNY"}
        text = render_draft(data)
        self.assertIn("住宿参考上限：1100元/人·天", text)
        self.assertNotIn("[来源", text)

    def test_malformed_saved_evidence_does_not_break_recommendation_display(self):
        for changes in ({"sources": None}, {"sources": [None]}, {"standard_citations": None},
                        {"standard_citations": {"lodging_cap": [None]}}):
            with self.subTest(changes=changes):
                data, _ = recommendation_fixture(lodging_standard="部级")
                data["recommendation_profile"].update(changes)
                line = next(line for line in main_text(render_draft(data)).splitlines()
                            if line.startswith("住宿参考上限"))
                self.assertNotIn("[来源", line)

    def test_missing_original_rows_keep_recommendation_without_inventing_cap_citation(self):
        data, _ = recommendation_fixture(omit_sources=True, lodging_standard="部级")
        profile = data["recommendation_profile"]
        self.assertEqual(profile["status"], "ok")
        self.assertEqual(profile["citation_status"], "partial")
        self.assertEqual(profile["standard_citations"]["lodging_cap"], [])
        text = render_draft(data)
        cap = next(line for line in text.splitlines() if line.startswith("住宿参考上限"))
        self.assertNotIn("[来源", cap)
        self.assertNotIn("原文摘录：", text)

    def test_grouped_baseline_preserves_previous_rendering(self):
        data, _ = recommendation_fixture(citation_mode="grouped", lodging_standard="部级")
        text = render_draft(data)
        self.assertIn('"title_categories"', text)
        self.assertEqual(text.count('"title_categories"'), 2)
        self.assertIn("标准：二类；对应人员：", text)
        self.assertIn("住宿参考上限：1100元/人·天 [来源3]", text)

    def test_hotel_only_has_one_original_source_without_transport_excerpts(self):
        data, _ = recommendation_fixture("hotel", lodging_standard="部级")
        text = render_draft(data)
        self.assertIn("住宿档次：部级\n", text)
        self.assertIn("住宿参考上限：1100元/人·天 [来源1]", text)
        self.assertEqual(text.count(":::details 政策依据"), 1)
        self.assertNotIn("第六条", text)
        self.assertNotIn("项目席别口径", text)

    def test_only_requested_transport_columns_and_rules_are_displayed(self):
        for mode, wanted, unwanted in (("train", "火车（含高铁", "飞 机："),
                                       ("flight", "飞 机：", "火车（含高铁")):
            with self.subTest(mode=mode):
                data, _ = recommendation_fixture("transport", trip_changes={"transport": mode})
                text = render_draft(data)
                self.assertIn(wanted, text)
                self.assertNotIn(unwanted, text)
                self.assertNotIn("轮船", text)
                self.assertNotIn("住宿档次", text)

    def test_hotel_identity_does_not_invent_lodging_grade(self):
        data, _ = recommendation_fixture("hotel", legacy_rules=True)
        text = render_draft(data)
        self.assertIsNone(data["recommendation_profile"]["lodging_standard"])
        self.assertIn("住宿档次：待核实", text)
        self.assertIn("住宿参考上限：待核实", text)
        self.assertNotIn("1100", text)

    def test_student_conditions_are_kept_including_hotel_only(self):
        for scope in ("combined", "hotel", "transport"):
            with self.subTest(scope=scope):
                data, _ = recommendation_fixture(scope, trip_changes={
                    "academic_title": None, "personnel_type": "student"})
                text = render_draft(data)
                self.assertIn("第三十五条", text)
                self.assertIn("教学科研活动、竞赛、社会实践及调研活动", text)
                self.assertIn("本次活动类型尚未确认", text)
                self.assertEqual(text.count("原文摘录：\n第三十五条"), 1)
                self.assertNotIn("标准：一类；对应人员：", text)
        data, _ = recommendation_fixture("hotel", trip_changes={"academic_title": None,
            "personnel_type": "student", "student_activity_eligible": True})
        self.assertNotIn("本次活动类型尚未确认", render_draft(data))

    def test_other_title_selects_its_actual_category_without_academician_row(self):
        data, _ = recommendation_fixture(trip_changes={"academic_title": "正高级"})
        text = render_draft(data)
        self.assertIn("交通类别：二类 [来源1]", text)
        self.assertIn("标准：二类；对应人员：", text)
        self.assertNotIn("标准：一类；对应人员：", text)
        self.assertIn("住宿档次：待核实", text)
        self.assertIn("住宿参考上限：待核实", text)
        self.assertNotIn("650元/人·天", text)

    def test_dual_posts_keep_the_priority_condition_without_unrelated_exceptions(self):
        data, _ = recommendation_fixture("transport", trip_changes={"academic_title": None,
            "technical_grade": 4, "management_grade": 5})
        text = render_draft(data)
        self.assertIn("就高", text)
        self.assertNotIn("随行一人", text)
        self.assertNotIn("夕发朝至", text)

    def test_student_outside_eligible_activities_does_not_get_a_default_category(self):
        data, _ = recommendation_fixture("hotel", trip_changes={"academic_title": None,
            "personnel_type": "student", "student_activity_eligible": False, "lodging_standard": "其他人员"})
        text = render_draft(data)
        self.assertIsNone(data["recommendation_profile"]["transport_standard"])
        self.assertIn("本次已明确不属于上述活动", text)
        self.assertNotIn("暂按三类", text)

    def test_fragments_with_identical_parent_and_quotes_share_a_number(self):
        data, _ = recommendation_fixture()
        profile = data["recommendation_profile"]
        first = copy.deepcopy(profile["citation_fragments"][0])
        first["fragment_id"] = "duplicate-fragment"
        profile["citation_fragments"].append(first)
        profile["fragment_citations"]["allowed_transport"][0] = "duplicate-fragment"
        text = render_draft(data)
        self.assertEqual(text.count("标准：一类；对应人员："), 1)
        self.assertEqual(text.count(":::details 政策依据 · 来源1\n"), 1)

    def test_changed_cap_or_quote_loses_the_reference(self):
        for mutation in ("cap", "unit", "date", "quote", "parent_text", "wrong_field", "parent_id", "hash_type"):
            with self.subTest(mutation=mutation):
                data, _ = recommendation_fixture(lodging_standard="部级")
                profile = data["recommendation_profile"]
                ident = profile["fragment_citations"]["lodging_cap"][0]
                fragment = next(f for f in profile["citation_fragments"] if f["fragment_id"] == ident)
                source = next(s for s in profile["sources"] if s["source_id"] == fragment["source_id"])
                if mutation == "cap":
                    profile["lodging_cap"] = "9999"
                elif mutation == "unit":
                    profile["lodging_unit"] = "room_night"
                elif mutation == "date":
                    profile["lodging_date"] = "2099-01-01"
                elif mutation == "quote":
                    fragment["quotes"] = ["北京"]
                elif mutation == "parent_text":
                    source["text"] = "部级：9999"
                elif mutation == "wrong_field":
                    profile["fragment_citations"]["lodging_cap"] = profile["fragment_citations"]["transport_standard"]
                elif mutation == "parent_id":
                    fragment["source_id"] = []
                else:
                    source["file_sha256"] = []
                line = next(line for line in main_text(render_draft(data)).splitlines()
                            if line.startswith("住宿参考上限"))
                self.assertNotIn("[来源", line)

    def test_ambiguous_category_rows_are_not_guessed(self):
        def duplicate_row(rows):
            row = copy.deepcopy(next(r for r in rows if r["chunk_id"] == "b0005-r1"))
            row["chunk_id"] = "duplicate-row"
            return rows + [row]
        data, _ = recommendation_fixture(row_transform=duplicate_row)
        line = next(line for line in main_text(render_draft(data)).splitlines() if line.startswith("交通类别"))
        self.assertNotIn("[来源", line)

    def test_broken_fragment_registry_stays_readable_without_fake_reference(self):
        for changes in ({"citation_fragments": None}, {"citation_fragments": [None]},
                        {"fragment_citations": None}, {"fragment_citations": {"lodging_cap": [None]}}):
            with self.subTest(changes=changes):
                data, _ = recommendation_fixture(lodging_standard="部级")
                data["recommendation_profile"].update(changes)
                line = next(line for line in main_text(render_draft(data)).splitlines()
                            if line.startswith("住宿参考上限"))
                self.assertNotIn("[来源", line)

    def test_changed_query_context_never_reuses_old_citations(self):
        for field, value in (("destination", "上海"), ("check_in", "2026-11-01"),
                             ("academic_title", "正高级")):
            with self.subTest(field=field):
                data, _ = recommendation_fixture()
                data["request"][field] = value
                self.assertNotIn("[来源", render_draft(data))

    def test_duplicate_source_or_fragment_id_is_rejected(self):
        for collection in ("sources", "citation_fragments"):
            with self.subTest(collection=collection):
                data, _ = recommendation_fixture()
                profile = data["recommendation_profile"]
                profile[collection].append(copy.deepcopy(profile[collection][0]))
                line = next(line for line in main_text(render_draft(data)).splitlines()
                            if line.startswith("高铁可乘"))
                self.assertNotIn("[来源", line)


if __name__ == "__main__":
    unittest.main()
