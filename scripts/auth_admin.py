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
from business_travel.invoice_store import setup_invoices
from business_travel.attachment_store import setup_attachments
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
    parser.add_argument("command", choices=["init", "init-runs", "init-resources", "init-invoices", "init-attachments", "create-user", "reset-password",
                                          "disable", "enable", "prune", "show-identity", "set-identity"])
    parser.add_argument("--username")
    parser.add_argument("--personnel-type", choices=["staff", "student", "invited"])
    parser.add_argument("--academic-title")
    parser.add_argument("--technical-grade", type=int)
    parser.add_argument("--management-grade", type=int)
    parser.add_argument("--lodging-standard", choices=["部级", "司局级", "其他人员"])
    parser.add_argument("--clear-transport", action="store_true")
    parser.add_argument("--clear-lodging", action="store_true")
    args = parser.parse_args()
    if args.command not in {"init", "init-runs", "init-resources", "init-invoices", "init-attachments", "prune"} and not args.username:
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
        elif args.command == "init-invoices":
            setup_invoices(settings)
        elif args.command == "init-attachments":
            setup_attachments(settings)
        elif args.command == "show-identity":
            import json
            print(json.dumps(store.get_personnel_by_username(args.username).model_dump(), ensure_ascii=False))
        elif args.command == "set-identity":
            from business_travel.personnel import PersonnelProfile
            current = store.get_personnel_by_username(args.username)
            payload = current.model_dump()
            if args.clear_transport:
                payload.update(personnel_type=None, academic_title=None, technical_grade=None, management_grade=None)
            if args.clear_lodging:
                payload["lodging_standard"] = None
            if args.personnel_type:
                payload["personnel_type"] = args.personnel_type
            if args.academic_title:
                payload["academic_title"] = args.academic_title
            if args.technical_grade is not None:
                payload["technical_grade"] = args.technical_grade
            if args.management_grade is not None:
                payload["management_grade"] = args.management_grade
            if args.lodging_standard:
                payload["lodging_standard"] = args.lodging_standard
            store.set_personnel(args.username, PersonnelProfile.model_validate(payload))
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
