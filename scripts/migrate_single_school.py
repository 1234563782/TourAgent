"""单学校迁移：默认只读预检；停服备份后显式 --apply。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg.rows import dict_row

from business_travel.settings import Settings
from business_travel.single_school_migration import migrate_schema, SchoolMigrationError


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["memory","policy"])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--service-stopped", action="store_true")
    parser.add_argument("--backup", type=Path, help="已校验的 pg_dump 备份文件；工具不会输出其内容")
    parser.add_argument("--digest-map",type=Path,action="append",default=[],help="政策包转换生成的摘要映射，可重复指定")
    args=parser.parse_args(argv)
    if args.apply and (not args.service_stopped or not args.backup or
                       not args.backup.is_file() or args.backup.stat().st_size == 0):
        parser.error("--apply 必须同时提供 --service-stopped 和非空 --backup 文件")
    try:
        settings=Settings.from_env()
        database=(settings.memory_database_url if args.target=="memory" else settings.policy_database_url)
        schema=settings.memory_schema if args.target=="memory" else "public"
        if not database:
            raise SchoolMigrationError("database_configuration_required")
        mappings=[]
        for path in args.digest_map:
            entries=json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(entries,list) or any(not isinstance(item,dict) for item in entries):
                raise SchoolMigrationError("invalid_policy_digest_mapping")
            mappings.extend(entries)
        with psycopg.connect(database,autocommit=True,row_factory=dict_row,
                             connect_timeout=settings.memory_connect_timeout) as conn:
            report=migrate_schema(conn,schema,kind=args.target,apply=args.apply,digest_mappings=mappings)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0
    except SchoolMigrationError as error:
        print(str(error),file=sys.stderr)
    except (psycopg.Error,OSError,ValueError):
        print("迁移未完成；当前事务已回滚。请检查数据库、备份、权限和冲突；未输出敏感内容。",file=sys.stderr)
    return 2


if __name__=="__main__":
    raise SystemExit(main())
