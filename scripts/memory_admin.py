"""PostgreSQL 记忆初始化、旧数据清点及显式迁移；不会输出记忆内容或凭证。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_travel.settings import Settings
from business_travel.memory_migration import MigrationError


def main(argv=None):
    parser = argparse.ArgumentParser(description="PostgreSQL 记忆管理（迁移前停止旧 CLI）")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("init", help="显式建立专用 schema 和官方检查点表")
    sub.add_parser("check", help="只读验证连接及记忆表")
    inspect = sub.add_parser("inspect", help="只读清点旧数据，仅展示标识和数量")
    migrate = sub.add_parser("migrate", help="显式导入一个旧用户，保留源文件")
    for command in (inspect, migrate):
        command.add_argument("--source-dir", type=Path)
        command.add_argument("--legacy-dir", type=Path)
    migrate.add_argument("--user", required=True)
    migrate.add_argument("--apply", action="store_true", help="实际导入；不指定则仅预检")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        from business_travel.memory_migration import import_snapshot, inventory, read_snapshot
        from business_travel.memory_pg import memory_connection, setup_memory
        if args.action == "init":
            setup_memory(settings)
            result = {"status": "initialized", "schema": settings.memory_schema}
        elif args.action == "check":
            with memory_connection(settings) as connection:
                for table in ("user_preferences", "trips", "memory_imports", "checkpoints",
                              "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
                    if connection.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"] is None:
                        raise ValueError("Memory schema is incomplete; run init")
            result = {"status": "ok", "backend": "postgres", "schema": settings.memory_schema}
        else:
            source = args.source_dir if args.source_dir is not None else settings.storage_dir
            legacy = args.legacy_dir if args.legacy_dir is not None else settings.legacy_memory_dir
            if args.action == "inspect":
                result = inventory(source, legacy)
            else:
                snapshot = read_snapshot(source, legacy, args.user)
                if not any(snapshot.summary().values()):
                    raise MigrationError("没有找到该用户的旧数据，请检查用户标识和源目录")
                result = import_snapshot(settings, snapshot) if args.apply else {
                    "status": "dry_run", **snapshot.summary()}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except MigrationError as error:
        print(f"迁移已停止：{error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"记忆操作失败（{type(error).__name__}）。请检查配置、数据库权限、旧数据格式或目标冲突；"
              "未覆盖源文件，不会自动重试。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
