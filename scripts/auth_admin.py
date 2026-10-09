"""可信本机管理：初始化、创建账号、重置密码、停用/启用、清理过期会话。"""

import argparse
import getpass
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from business_travel.auth import AuthSettings, AuthStore, setup_auth
from business_travel.memory_pg import setup_memory
from business_travel.chat_runs import setup_chat_runs
from business_travel.resources import setup_resources
from business_travel.settings import Settings


def prompt_password():
    # Never fall back to an echoed stdin password.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        password = getpass.getpass("密码（12-128 个字符）: ")
        confirmation = getpass.getpass("再次输入密码: ")
    if password != confirmation:
        raise ValueError("两次密码不一致")
    return password


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "init-runs", "init-resources", "create-user", "reset-password",
                                          "disable", "enable", "prune"])
    parser.add_argument("--username")
    args = parser.parse_args()
    if args.command not in {"init", "init-runs", "init-resources", "prune"} and not args.username:
        parser.error("此命令需要 --username")
    try:
        settings = Settings.from_env()
        store = AuthStore(settings, AuthSettings.from_env())
        if args.command == "init":
            setup_memory(settings)
            setup_auth(settings)
        elif args.command == "init-runs":
            setup_chat_runs(settings)
        elif args.command == "init-resources":
            setup_resources(settings)
        elif args.command == "create-user":
            store.create_user(args.username, prompt_password())
        elif args.command == "reset-password":
            store.manage_user(args.username, password=prompt_password())
        elif args.command in {"enable", "disable"}:
            store.manage_user(args.username, active=args.command == "enable")
        else:
            store.prune()
    except psycopg.errors.UniqueViolation:
        print("账号已存在，未覆盖。", file=sys.stderr)
        return 2
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (psycopg.Error, OSError, getpass.GetPassWarning):
        print("操作失败；检查数据库、初始化状态及交互终端。未输出敏感信息。", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        return 2
    print("操作完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
