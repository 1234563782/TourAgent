#!/usr/bin/env python
"""输出运行时使用的全局 HNSW 建索引 SQL；不连接数据库。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_travel.policy_hnsw import render_index_ddl


def render_ddl(dimensions: int, *, m: int = 16, ef_construction: int = 128) -> str:
    return render_index_ddl(dimensions, m=m, ef_construction=ef_construction)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成全局 policy_chunks HNSW DDL。")
    parser.add_argument("--dimensions", type=int, required=True)
    parser.add_argument("--m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=128)
    args = parser.parse_args()
    print(render_ddl(args.dimensions, m=args.m, ef_construction=args.ef_construction))
