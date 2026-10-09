#!/usr/bin/env python
"""输出可选 HNSW 建索引 SQL；不连接数据库，也不改变默认精确检索路径。"""

import argparse


def render_ddl(document_id: int, dimensions: int) -> str:
    if type(document_id) is not int or document_id < 1:
        raise ValueError("document_id 必须是正整数")
    if type(dimensions) is not int or not 1 <= dimensions <= 2000:
        raise ValueError("此可选 vector HNSW 模板仅支持 1 至 2000 维")
    return (
        f"CREATE INDEX IF NOT EXISTS policy_chunks_doc_{document_id}_hnsw "
        f"ON policy_chunks USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
        f"WHERE document_id = {document_id};"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成按文档隔离的可选 HNSW DDL；仅供后续性能对比。")
    parser.add_argument("--document-id", type=int, required=True)
    parser.add_argument("--dimensions", type=int, required=True)
    args = parser.parse_args()
    print(render_ddl(args.document_id, args.dimensions))
