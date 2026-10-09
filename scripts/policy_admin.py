#!/usr/bin/env python
"""政策解析、核对、导入、发布和只读检索入口；不自动读取或发布模拟制度。"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_travel.policy_documents import PolicyBundle, parse_docx
from business_travel.policy_rules import ExpenseFacts, ReviewedRules
from business_travel.settings import Settings


def parser():
    result = argparse.ArgumentParser(description="差旅政策管理；prepare 为纯离线操作，其他服务操作均须显式执行。")
    sub = result.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Word 解析成待核对 JSON，不连接服务")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--company", required=True)
    prepare.add_argument("--policy-key", default="travel")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--effective-from", type=date.fromisoformat, required=True)
    prepare.add_argument("--effective-to", type=date.fromisoformat)
    prepare.add_argument("--rules", type=Path, help="人工整理的结构化规则草稿")
    prepare.add_argument("--chunk-size", type=int, default=1400)
    package = sub.add_parser("prepare-package", help="离线组装正文和附表，不连接服务")
    package.add_argument("--manifest", type=Path, required=True)
    package.add_argument("--company", required=True)
    package.add_argument("--output", type=Path, required=True)
    sub.add_parser("check-config", help="仅显示缺失配置键名，不联网")
    sub.add_parser("init-db", help="显式创建 vector 扩展及政策表，需要数据库 DDL 权限")
    stage = sub.add_parser("stage", help="发送正文给配置的 Embedding 服务，并写入数据库草稿")
    stage.add_argument("--bundle", type=Path, required=True)
    publish = sub.add_parser("publish", help="确认核对后发布数据库中的草稿")
    publish.add_argument("--document-id", type=int, required=True)
    publish.add_argument("--reviewed-sha256", required=True, help="核对过的导入包 SHA256")
    publish.add_argument("--supersede", action="store_true", help="将重叠旧版本截止日设为新版本生效前一天")
    search = sub.add_parser("search", help="检索当前发布政策")
    search.add_argument("--question", required=True)
    check = sub.add_parser("check", help="按单日或单晚事实执行规则校验")
    check.add_argument("--facts", type=Path, required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare-package":
            from business_travel.policy_package import prepare_package
            bundle = prepare_package(args.manifest, company_id=args.company)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(bundle.model_dump_json(indent=2))
            output = {"status": "prepared_not_published", "chunks": len(bundle.chunks),
                      "lodging_rows": len(bundle.lodging_rows), "bundle_sha256": bundle.digest(),
                      "warnings": bundle.warnings}
        elif args.command == "prepare":
            if args.output.exists() or args.output.resolve() == args.source.resolve():
                raise ValueError("输出文件已存在；请选择新的导入包路径，避免覆盖。")
            bundle = parse_docx(args.source, company_id=args.company, policy_key=args.policy_key,
                                version=args.version, effective_from=args.effective_from,
                                effective_to=args.effective_to, chunk_size=args.chunk_size)
            if args.rules:
                rules = ReviewedRules.model_validate_json(args.rules.read_text(encoding="utf-8"))
                clauses = {chunk.clause for chunk in bundle.chunks}
                if any(clause not in clauses for group in rules.source_clauses.values() for clause in group):
                    raise ValueError("结构化规则引用了文档中不存在的条款。")
                bundle.rules = rules.model_dump(mode="json")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(bundle.model_dump_json(indent=2))
            output = {"status": "prepared_not_published", "chunks": len(bundle.chunks),
                      "table_rows": sum(chunk.kind == "table_row" for chunk in bundle.chunks),
                      "bundle_sha256": bundle.digest(), "warnings": bundle.warnings}
        else:
            settings = Settings.from_env()
            if args.command == "check-config":
                missing = settings.missing_policy_config()
                print(json.dumps({"backend": settings.policy_backend, "missing_variables": missing,
                                  "database_verified": False, "embedding_verified": False}, ensure_ascii=False, indent=2))
                return 2 if missing else 0
            from business_travel.policy_pg import PostgresPolicyStore
            store = PostgresPolicyStore(settings)
            if args.command == "init-db":
                output = store.initialize()
            elif args.command == "stage":
                output = store.stage(PolicyBundle.model_validate_json(args.bundle.read_text(encoding="utf-8")))
            elif args.command == "publish":
                output = store.publish(args.document_id, args.reviewed_sha256, supersede=args.supersede)
            elif args.command == "search":
                output = store.search(args.question)
            else:
                output = store.check(ExpenseFacts.model_validate_json(args.facts.read_text(encoding="utf-8")))
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 2 if output.get("status") in {"error", "unavailable"} else 0
    except Exception as error:
        from business_travel.policy_pg import PolicyError
        # ValidationError and OS/HTTP errors can contain input values, paths or credentials.
        message = str(error) if isinstance(error, PolicyError) else "操作失败，请检查文件格式、参数、配置和权限。"
        print(json.dumps({"status": "error", "reason": message, "category": type(error).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
