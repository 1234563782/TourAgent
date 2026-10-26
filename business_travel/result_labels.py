"""Presentation vocabulary only; policy amounts and decisions stay in the rule engine."""

KIND_LABELS = {"hotel": "酒店", "flight": "航班", "train": "火车",
               "transport": "交通", "meal": "餐补", "local_transport": "市内交通"}
QUOTE_LABELS = {"ticket": "每张票", "night": "每晚", "unknown": "待核实"}
POLICY_STATUS_LABELS = {
    "unverified": "尚未核实", "insufficient_information": "信息不足，不能确认合规",
    "manual_review": "需要人工核验", "over_standard": "超出所核对的一般标准",
    "over_cap": "参考报价超出限额",
    "within_checked_rules": "在本次所核对的规则范围内，不代表完整合规或批准",
}
POLICY_REASON_LABELS = {
    "transport_class_mapping_required": "当前政策尚未明确匹配该席别名称，需要核实适用标准",
    "lodging_standard_required": "住宿身份尚未确定，不能由院士、职称或交通类别推出；请单独确认部级、司局级或其他人员",
    "lodging_location_required": "需明确附表对应的省市或具体区县，不能把未知地点自动归为其他地区",
    "lodging_region_ambiguous": "地点匹配到不同标准，请补充具体地区",
    "university_transport_standard": "核对学校一般交通类别；超出一般标准不排除正文规定的随行、全软席等例外",
    "university_lodging_cap": "按住宿附表对应地区、人员档次和旺季日期核对元/人·天标准",
    "university_quote_basis_required": "需核实日期、人民币含税金额和个人每天住宿口径；单人入住才可对应每间每晚",
    "university_foreign_currency_projection": "你写的是外币金额，限额按人民币比对；请提供人民币金额或折算依据",
    "university_tax_exclusive_projection": "你明确写的是不含税金额，不能直接与含税限额比对；请提供含税金额",
    "personal_lodging_amount_required": "合住不能自动除以人数，请核实个人每天实际住宿费用",
    "university_exception_conditions": "此场景涉及特殊适用条件，请查阅对应条款并向学校财务核实；系统不办理审批",
    "university_conference_lodging_exception": "主办方统一安排且自理的会议培训住宿需有效证明；不按一般限额直接否定",
    "university_meeting_meal_excluded": "会议培训统一供餐期间不发餐补，仅核对在途日期的补助",
    "university_calendar_day_meal": "按所述自然天的地区一般餐补标准；连续在途超过12小时的加发只在提供同一张票据的出发、到达时间时单独计算",
    "university_no_duplicate_local_transport": "校外单位已承担市内交通费用，不重复领取或报销",
    "university_local_transport_receipts": "选择凭据报销需核实实际票据，不同时发放包干补助",
    "university_local_transport_daily": "核对所述日期的市内交通包干标准；不代表完整报销批准",
    "each_night_requires_individual_verified_quote": "连续住宿需要每晚分别核实的报价，请补充逐晚含税价格",
    "confirmed_transport_class_required": "请核实交通方式和席别后再核对标准",
    "required_facts_missing": "缺少适用规则所需的信息，请补充后重新核对",
    "reviewed_database_rules_required": "尚未接入经审核的差旅规则，请联系管理员",
    "estimate_mode_required": "本项含假设或多晚费用，请将 TRAVEL_POLICY_CHECK_MODE 设为 estimate 后重新测算",
    "reviewed_rules_unavailable": "当前政策缺少经审核的规则，请联系管理员",
    "policy_unconfigured": "差旅政策尚未配置，请联系管理员",
    "policy_outside_effective_period": "现有政策不适用于该日期，请核实有效版本",
    "no_published_policy": "当前尚无已发布政策，请联系管理员发布制度资料",
    "lodging_date_required": "该住宿档次存在季节差价，需要实际入住日期",
    "enterprise_policy_documents_unconfigured": "组织政策资料尚未配置，请联系管理员",
    "no_applicable_cap": "尚未找到适用限额，请核实职级、城市及费用科目",
    "price_or_currency_unconfirmed": "报价或币种尚未确认，请核实后再判断",
    "price_basis_unconfirmed": "计价口径尚未确认，请核实每张票或每晚的价格",
    "quoted_amount_exceeds_cap": "参考报价超过所核对的限额，需调整方案或核实特殊情况",
    "indicative_amount_within_cap_not_final_approval": "参考金额在所核对的限额内，不代表最终价格或批准",
    "policy_check_failed": "政策核验暂时失败，请稍后重新核实",
    "verify_special_approval_scope": "请人工核实专项审批覆盖的人员、事项、期限和额度",
    "outside_domestic_business_scope": "不属于当前大陆因公出差规则范围，需要另行核验",
    "long_assignment_requires_separate_plan": "长期驻场需要核对单独批准的驻场方案",
    "special_room_arrangement": "特殊入住安排需要人工核验",
    "city_mapping_required": "城市所属政策类别尚未核实，请确认实际出差城市",
    "shared_room_conditions_not_met": "当前合住条件不满足一般规则，需要人工核实",
    "nightly_tax_inclusive_quote_required": "需要日期、币种和含税口径均已核实的每间每晚报价",
    "hotel_nightly_cap_comparison": "按适用的每间每晚住宿限额比较",
    "transport_class_requires_special_approval": "所选席别不在当前职级的一般标准内，需要核实专项审批",
    "explicit_flight_approval_required": "需核实是否已获乘机批准，当前不能自动放行",
    "railway_preferred_verify_flight_exception": "当前条件下优先适用铁路规则，乘机例外需要人工核实",
    "transport_class_checked_not_budget_or_approval": "仅核对交通席别，未核对总预算或批准状态",
    "allowance_region_mapping_required": "补助地区尚未匹配适用标准，请核实地区",
    "daily_meal_calculation": "按当日餐补标准与免费餐次计算",
    "daily_local_transport_calculation": "按当日公务出行、接送和已报费用计算",
    "unsupported_user_facts": "部分字段未通过用户原话证据检查，未提交规则核验",
}


def policy_reason_label(reason):
    return POLICY_REASON_LABELS.get(
        reason, "尚未取得可解释的核验依据，请重新核实或联系管理员，不能据此认定合规")
