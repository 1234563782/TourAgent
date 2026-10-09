"""从真实回答和人工复核结论生成中文验收报告，不自动把执行成功算作通过。"""

import argparse
import json
from collections import Counter
from pathlib import Path


def render(records, reviews, retrieval_cases):
    if {row["id"] for row in records} != set(reviews):
        raise ValueError("每组真实案例必须有且仅有对应的复核结论")
    counts = Counter(reviews[row["id"]]["status"] for row in records)
    turns = sum(len(row["turns"]) for row in records)
    lines = [
        "# 企业差旅助手验收报告",
        "",
        "验收日期：2026-09-08（Asia/Shanghai）。本报告是首轮验收记录，不是全部通过或上线批准。",
        "",
        "## 1. 总结",
        "",
        f"真实对话共 {len(records)} 组、{turns} 轮。人工复核：" +
        "，".join(f"{status} {count} 组" for status, count in counts.items()) + "。",
        "",
        "- 离线 unittest：初轮118/118通过；新增3项报告回归后最终121/121通过；pip check无依赖冲突。",
        "- 真实政策检索：30/30 来源覆盖通过，不能等同于30个真实模型回答正确。",
        "- 途牛航班独立探测：北京至上海2026-09-10，10条候选，10条有价，10条日期匹配。",
        "- 规划轨迹中：去返高铁分别10条候选，酒店8条候选；完整规划没有成功产出草案。",
        "- 地图：AMAP_API_KEY未配置，真实通勤查询阻塞，不能判定地图服务通过。",
        "- 本轮未修改生产业务代码或真实.env；发现的问题尚未修复、尚未复测。",
        "",
        "## 2. 怎么验收",
        "",
        "1. 读取项目代码、契约、已发布模拟政策、现有测试及历史记录，确认Python/pip和项目.venv。",
        "2. 重跑全部离线测试与依赖检查；离线模型/供应商使用脚本化或合成数据，不代表真实服务。",
        "3. 使用现有verify_policy脚本逐题检索，检查要求的条款包含在最终证据中且上下文未截断。",
        "4. 使用与CLI相同的build_assistant入口、真实配置模型、真实pgvector/Embedding和供应商。",
        "5. 每组单独建测试用户和InMemorySaver检查点；同组多轮共享上下文。记忆写入独立测试SQLite，"
        "不访问用户已有记忆。接受/拒绝确认由测试脚本模拟，仅在出现interrupt后执行。",
        "6. 包装模型的invoke记录supervisor/planner/policy的实际调用和后续ToolMessage，保存最终回答。"
        "工具调用意图不等同于执行成功；通过后续工具返回核对结果，多个工具调用被拒绝时不计为执行。",
        "7. 人工逐项核对数字、版本日期、缺失事实、主子派发、规则调用、写入结果和越权边界。"
        "脚本的completed只表示正常返回，业务失败仍单列。",
        "8. 所有操作均为查询或隔离测试记忆保存，没有预订、付款、取消、政策发布或真实审批。",
        "",
        "### 复现命令",
        "",
        "在TourAgent项目目录运行；真实验收会调用收费或有额度的已配置API。输出目录必须是新目录。",
        "",
        "```powershell",
        r".\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v",
        r".\.venv\Scripts\python.exe -m pip check",
        r".\.venv\Scripts\python.exe -X utf8 travel_cli.py --check-config",
        r".\.venv\Scripts\python.exe -X utf8 scripts/verify_policy.py --cases policies/demo-retrieval-cases.json",
        r".\.venv\Scripts\python.exe -X utf8 scripts/verify_tuniu.py --kind flight --origin 北京 --destination 上海 --date 2026-09-10",
        r".\.venv\Scripts\python.exe -X utf8 scripts/acceptance_chat.py --cases tests/acceptance_cases.json --output .verification/acceptance-replay-main",
        r".\.venv\Scripts\python.exe -X utf8 scripts/acceptance_chat.py --cases tests/acceptance_rules_cases.json --output .verification/acceptance-replay-rules",
        "```",
        "",
        "模型非确定性及外部数据变化可能改变再次运行的结果；不得用后续成功覆盖本轮失败证据。",
        "",
        "## 3. 案例结果总表",
        "",
        "| 案例 | 结果 | 判定依据 |",
        "| --- | --- | --- |",
    ]
    for row in records:
        review = reviews[row["id"]]
        lines.append(f'| {row["id"]} | {review["status"]} | {review["reason"].replace("|", "/")} |')
    lines.extend(["", "## 4. 问题、原始回答与调用轨迹", "",
                  "下列回答逐字来自本轮保存的最终输出，保留其中的错误、不准确表述和原格式。"])
    for index, row in enumerate(records, 1):
        review = reviews[row["id"]]
        lines.extend(["", f"### {index}. {row['id']}：{review['status']}", "",
                      f"预期：{row['expected']}", "", f"复核：{review['reason']}"])
        for number, turn in enumerate(row["turns"], 1):
            lines.extend(["", f"#### 第{number}轮", "", "**提问原文**", "", turn["question"], "",
                          f"耗时：{turn['seconds']}秒；执行状态：{turn['status']}。"])
            if "confirmation_response" in turn:
                lines.append("测试确认：" + ("接受" if turn["confirmation_response"] else "拒绝") + "。")
            if "error_category" in turn:
                lines.append("异常类别：" + turn["error_category"])
            lines.extend(["", "**回答原文**", "", str(turn.get("answer", "未取得最终回答。")),
                          "", "**模型工具调用记录**", ""])
            calls = [
                {"agent": event["role"], "tool": call["name"], "args": call["args"]}
                for event in turn["events"] for call in event["tool_calls"]
            ]
            if calls:
                lines.extend(["```json", json.dumps(calls, ensure_ascii=False, indent=2), "```"])
            else:
                lines.append("本轮没有工具调用。")
            lines.append("")
            lines.append("测试记忆中的偏好：" + json.dumps(
                turn["memory"]["preferences"], ensure_ascii=False) + "。")
    lines.extend(["", "## 5. 30题检索覆盖明细", "",
                  "这部分只执行检索，不请求模型生成回答；因此“回答”均为不适用。每题所列条款均命中，"
                  "未发现上下文截断。最终回答质量由上面的真实对话案例另行评估。", "",
                  "| 编号 | 检索问题 | 要求条款 | 本轮结果 |", "| --- | --- | --- | --- |"])
    for index, case in enumerate(retrieval_cases, 1):
        lines.append(f"| {index} | {case['question']} | {', '.join(case['clauses'])} | 命中 |")
    lines.extend([
        "", "## 6. 问题处理优先级与未验收范围", "",
        "1. 优先修复偏好工具的模型可见参数契约。当前list[dict]未表达type/value必填结构，"
        "真实模型多种猜测均被invalid_preferences拒绝，确认流程无法触达。",
        "2. 补强主助手数字核验及事实转交约束：费用变化必须重新核验；明确提供的出差天数不能遗漏；"
        "未确认的币种、计价和核验状态不得自行补全。",
        "3. 为子图异常补充脱敏可观测性。本轮只有通用Tool failed，不能断言是模型、网络、地图或规则错误；"
        "需先记录具体异常类别和阶段再定位，不能凭缺地图Key推断整条规划失败原因。",
        "4. 统一最终政策回答的版本、文件名和条款引用；子Agent持有来源不代表用户最终能看到来源。",
        "5. 用户补充AMAP_API_KEY后，补验真实地址解析与通勤估算；不绕过或伪造配置。",
        "6. 完成修复后原样回归本轮失败案例，再补验真实草案保存接受/拒绝、改日期重规划、"
        "跨进程恢复、故障中断恢复、更多城市职级及长上下文。当前部分仅有离线测试覆盖。",
        "",
        "本轮不是性能压测、生产安全审计或法规审查；不覆盖企业身份认证、ACL/RLS、多租户上线验收、"
        "预订支付及财务审批。当前使用模拟制度，不代表正式企业制度已获审核。",
        "",
        "## 7. 证据文件", "",
        "- 原始主流程回答/工具返回：`.verification/acceptance-20260908-run1/results.json`。",
        "- 原始扩展规则回答/工具返回：`.verification/acceptance-20260908-rules/results.json`。",
        "- 人工判定：`tests/acceptance_review_20260908.json`。",
        "- 固定问题：`tests/acceptance_cases.json`、`tests/acceptance_rules_cases.json`。",
        "- 检索问题：`policies/demo-retrieval-cases.json`。",
        "- 自动化脚本：`scripts/acceptance_chat.py`、`scripts/render_acceptance_report.py`。",
        "",
        "原始轨迹含模拟政策原文与供应商查询结果，仅保存在本地项目；未记录系统提示词或API密钥。"
        "分享前仍应审查业务数据。离线测试、检索汇总和航班汇总由本轮终端执行获得，报告保留结果，"
        "未另存完整终端日志。模型具体版本指纹和内部异常类别未在首轮采集，属于证据限制。",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--retrieval-cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = [row for path in args.results
               for row in json.loads(path.read_text(encoding="utf-8"))]
    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))
    cases = json.loads(args.retrieval_cases.read_text(encoding="utf-8"))
    report = render(records, reviews, cases)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(report)
    print(json.dumps({"groups": len(records), "turns": sum(len(r["turns"]) for r in records),
                      "reviews": dict(Counter(item["status"] for item in reviews.values()))},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
