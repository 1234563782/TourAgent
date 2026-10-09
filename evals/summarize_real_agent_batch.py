"""Create a human-readable failure summary for a real-agent batch."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".verification" / "eval-batch-qwen-20260928.jsonl"
EXPECTED = ROOT / "evals" / "review" / "expected_drafts.jsonl"
OUTPUT = ROOT / ".verification" / "eval-batch-qwen-20260928-failure-summary.md"


def load_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=SOURCE)
    parser.add_argument("--expected", type=Path, default=EXPECTED)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    args.input = args.input.resolve()
    args.expected = args.expected.resolve()
    args.output = args.output.resolve()

    results = load_jsonl(args.input)
    expected = {row["candidate_id"]: row for row in load_jsonl(args.expected)}
    groups = defaultdict(list)
    issue_counts = Counter()

    for result in results:
        candidate = expected.get(result["candidate_id"], {})
        conversation = candidate.get("conversation", [])
        for observed in result.get("turns", []):
            turn_index = observed["turn"] - 1
            user_input = ""
            if 0 <= turn_index < len(conversation):
                user_input = conversation[turn_index].get("content", "")

            tool_issues = [
                f'{item.get("name")}: {item.get("status")}'
                for item in observed.get("tool_statuses_seen", [])
                if item.get("status") in {"error", "unavailable", "failed"}
            ]
            failure_code = observed.get("failure_code")
            issue = failure_code or (
                "tool_failure:" + ", ".join(tool_issues) if tool_issues else None
            )
            if not issue and observed["turn"] > result.get("observed_turns", 0):
                issue = "missing_turn"
            if not issue:
                continue

            issue_counts[issue] += 1
            groups[issue].append(
                {
                    "candidate_id": result["candidate_id"],
                    "family": result.get("family"),
                    "turn": observed["turn"],
                    "user_input": user_input,
                    "answer": observed.get("answer", ""),
                    "tools": observed.get("tool_calls_seen", []),
                    "tool_statuses": observed.get("tool_statuses_seen", []),
                }
            )

    lines = [
        "# Real Agent Batch Failure Summary",
        "",
        f"- Input: `{args.input.relative_to(ROOT.resolve())}`",
        f"- Candidates: {len(results)}",
        f"- Failure observations: {sum(issue_counts.values())}",
        "- Scope: exploratory empty-state run; not gold and not an accuracy score.",
        "",
        "## Distribution",
        "",
        "| Failure type | Count |",
        "|---|---:|",
    ]
    for issue, count in issue_counts.most_common():
        lines.append(f"| `{issue}` | {count} |")

    for issue, items in groups.items():
        lines.extend(["", f"## `{issue}`", ""])
        for item in items:
            lines.extend(
                [
                    f"### {item['candidate_id']} / turn {item['turn']}",
                    f"- Family: `{item['family']}`",
                    f"- User: {item['user_input'] or '(not available)'}",
                    f"- Tools: `{', '.join(item['tools']) or 'none'}`",
                    f"- Tool statuses: `{json.dumps(item['tool_statuses'], ensure_ascii=False)}`",
                    "- Agent answer:",
                    "",
                    "```text",
                    item["answer"].strip() or "(empty)",
                    "```",
                    "",
                ]
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.output}")
    print(json.dumps(dict(issue_counts), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
