"""生成本轮中文业务复测报告，保留原始问题、回答及人工判定。"""

import argparse
import json
from collections import Counter
from pathlib import Path


def render(records, reviews, run_date, evidence_paths, structured=False):
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)) or set(ids) != set(reviews):
        raise ValueError("每个案例必须有唯一对应的复核结论")
    counts = Counter(review["status"] for review in reviews.values())
    lines = [
        "# 企业差旅助手结构化槽位验收" if structured else "# 企业差旅助手代码约束修复复测",
        "",
        f"验收日期：{run_date}（Asia/Shanghai）。不是上线或财务审批结论。",
        "",
        "## 结果",
        "",
        f"原固定案例共{len(records)}组、{sum(len(row['turns']) for row in records)}轮；"
        + "，".join(f"{key}{value}组" for key, value in counts.items()) + "。",
        "",
        "## 验收方法",
        "",
        ("1. 用脚本化模型复现槽位遗漏、短引用和候选截断问题，校验原子合并、版本、失败拒绝及时间冲突。"
         if structured else "1. 用脚本化模型复现结论改写、无依据事实、行程时间冲突三个失败，再接入约束并回归。"),
        "2. 执行离线全量 unittest；真实数据库测试是否运行以本轮进度记录为准。",
        "3. 原样执行两份固定案例，使用真实模型、政策检索和差旅服务；每组单独隔离测试记忆。",
        "4. 确认仅在 LangGraph interrupt 出现后模拟接受或拒绝；不写正式用户记忆、不预订。",
        "5. 对照最终回答、实际工具结果和记忆逐项人工判定；completed不等于业务通过。",
        "",
        "## 判定总表",
        "",
        "| 案例 | 判定 | 原因 |",
        "| --- | --- | --- |",
    ]
    for record in records:
        review = reviews[record["id"]]
        lines.append(f"| {record['id']} | {review['status']} | {review['reason'].replace('|', '/')} |")
    lines.extend(["", "## 原始问题与回答", "", "以下保留实际输出，包括其中的不足。"])
    for record in records:
        lines.extend(["", f"### {record['id']}", "", "预期：" + record["expected"]])
        for number, turn in enumerate(record["turns"], 1):
            lines.extend(["", f"#### 第{number}轮", "", "提问：", "", turn["question"],
                          "", "回答：", "", turn.get("answer", "没有取得最终回答"),
                          "", f"执行状态：{turn['status']}；耗时：{turn['seconds']}秒。"])
            if "error_category" in turn:
                lines.append("异常类别：" + turn["error_category"])
            if "confirmation_response" in turn:
                lines.append("实际进入确认中断；测试选择：" + ("接受" if turn["confirmation_response"] else "拒绝"))
            if structured:
                slots = {key: {name: value for name, value in subject.items()
                               if name in {"id", "revision", "values"}}
                         for key, subject in turn.get("slots", {}).items()}
                lines.extend(["", "已验证槽位及错误：", "", "```json",
                              json.dumps({"slots": slots, "slot_error": turn.get("slot_error")},
                                         ensure_ascii=False, indent=2), "```"])
            calls = [{"agent": event["role"], "tool": call["name"], "args": call["args"]}
                     for event in turn["events"] for call in event["tool_calls"]]
            lines.extend(["", "模型调用轨迹（调用意图不等同于执行成功）：", "", "```json",
                          json.dumps(calls, ensure_ascii=False, indent=2), "```", "",
                          "已保存行程：" + json.dumps(turn["memory"].get("trip_history", []), ensure_ascii=False)])
    lines.extend([
        "", "## 剩余边界", "",
        "- 字段原话匹配是保守筛查，不是通用语义证明；复杂数字、否定和跨轮纠正仍需专门验证。",
        ("- 入口通过验证后由主图强制派发政策/规划；意图识别本身仍可能出错。天气工具结果由代码呈现，普通对话仍依赖模型。"
         if structured else "- 确定性呈现仅覆盖本轮实际调用的政策、规划和保存工具；尚无强制政策意图路由。"),
        "- 长期偏好不再保存；已确认行程的写入仍必须收到确认中断的True。",
        "- 时间校验不推断缺失时刻，也未覆盖所有机场换乘、跨时区和入住营业规则。",
        "- 本报告的业务结论限于列出的固定案例；数据库与单元测试数字见本轮验收摘要，不以历史成功替代本次结果。",
        "", "## 证据", "",
        *[f"- `{path}`" for path in evidence_paths],
        "", "原始工具返回见上述本地JSON；报告不包含密钥，分享前仍需审查业务数据。", "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, nargs="+", required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--structured", action="store_true", help="使用结构化槽位架构的说明及证据格式")
    args = parser.parse_args()
    records = [record for path in args.results for record in json.loads(path.read_text(encoding="utf-8"))]
    reviews = json.loads(args.reviews.read_text(encoding="utf-8"))
    report = render(records, reviews, args.date, [str(path) for path in args.results], args.structured)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(report)
    print(json.dumps({"groups": len(records), "reviews": dict(Counter(r["status"] for r in reviews.values()))},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
