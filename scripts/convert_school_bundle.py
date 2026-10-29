"""离线转换旧政策包及摘要映射；不覆盖原文件，不访问数据库。"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from business_travel.policy_documents import PolicyBundle


def convert(value):
    legacy=dict(value)
    namespace=legacy.pop("company_id",None)
    if not isinstance(namespace,str):
        raise ValueError("Expected a legacy policy bundle")
    bundle=PolicyBundle.model_validate(legacy)
    excluded={"sources","lodging_rows","adoption_note"} if bundle.schema_version==1 else set()
    canonical=json.loads(bundle.model_dump_json(exclude=excluded))
    old={"schema_version":canonical.pop("schema_version"),"company_id":namespace,**canonical}
    old_digest=hashlib.sha256(json.dumps(old,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()
    return bundle,{"policy_key":bundle.policy_key,"version":bundle.version,
                   "old_digest":old_digest,"new_digest":bundle.digest()}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--receipt",type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.output.exists() or args.receipt.exists() or args.output.resolve()==args.receipt.resolve():
            raise ValueError("Output exists or paths overlap")
        bundle,receipt=convert(json.loads(args.source.read_text(encoding='utf-8-sig')))
        # Exclusive creation protects existing artifacts even if another process races.
        with args.output.open('x',encoding='utf-8') as stream:
            stream.write(bundle.model_dump_json(indent=2))
        with args.receipt.open('x',encoding='utf-8') as stream:
            json.dump([receipt],stream,ensure_ascii=False,indent=2)
        print(json.dumps(receipt,ensure_ascii=False))
        return 0
    except (OSError,ValueError,TypeError):
        print("转换失败；检查原包、输出目录及是否已存在。源文件未修改。",file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
