"""Versioned import bundles for native DOCX policy documents."""

import hashlib
import re
from datetime import date
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from pydantic import Field, model_validator

from .contracts import Contract


class PolicyChunk(Contract):
    id: str
    section: str
    clause: str = ""
    text: str = Field(min_length=1)
    kind: str = "paragraph"
    table: dict | None = None
    related_ids: list[str] = Field(default_factory=list)

class PolicySource(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    source: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    role: str = Field(min_length=1)


class PolicyBundle(Contract):
    schema_version: int = 1
    policy_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    version: str = Field(min_length=1, max_length=100)
    effective_from: date
    effective_to: date | None = None
    source: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    chunks: list[PolicyChunk] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)
    rules: dict | None = None
    sources: list[PolicySource] = Field(default_factory=list)
    lodging_rows: list[dict] = Field(default_factory=list)
    adoption_note: str = ""

    @model_validator(mode="after")
    def valid_bundle(self):
        if self.schema_version not in (1, 2):
            raise ValueError("Unsupported policy bundle schema")
        if self.schema_version == 2:
            source_map = {source.id: source for source in self.sources}
            if len(source_map) != len(self.sources) or not self.sources or not self.adoption_note:
                raise ValueError("Policy package requires unique sources and an adoption note")
            for chunk in self.chunks:
                provenance = (chunk.table or {}).get("_provenance", {})
                source = source_map.get(provenance.get("source_id"))
                if not source or provenance.get("source") != source.source or provenance.get("sha256") != source.sha256:
                    raise ValueError("Chunk source does not match the package manifest")
            if self.source_sha256 != sources_digest(self.sources):
                raise ValueError("Policy package source digest mismatch")
            from .policy_lodging import LodgingRow
            rows = [LodgingRow.model_validate(row) for row in self.lodging_rows]
            by_id = {chunk.id: chunk for chunk in self.chunks}
            if len({row.id for row in rows}) != len(rows):
                raise ValueError("Duplicate lodging rows")
            for row in rows:
                chunk = by_id.get(row.id)
                if (not chunk or row.source_id not in source_map
                        or (chunk.table or {}).get("lodging_row") != row.model_dump(mode="json")
                        or chunk.table["_provenance"]["source_id"] != row.source_id):
                    raise ValueError("Lodging row does not match its source chunk")
        elif self.sources or self.lodging_rows:
            raise ValueError("Multiple sources require package schema 2")
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("Invalid policy effective period")
        ids = [c.id for c in self.chunks]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate policy chunk IDs")
        if any(ref not in ids for c in self.chunks for ref in c.related_ids):
            raise ValueError("Unknown related policy chunk")
        return self

    def digest(self) -> str:
        excluded = {"sources", "lodging_rows", "adoption_note"} if self.schema_version == 1 else set()
        return hashlib.sha256(self.model_dump_json(exclude=excluded).encode("utf-8")).hexdigest()

def sources_digest(sources: list[PolicySource]) -> str:
    import json
    return hashlib.sha256(json.dumps([s.model_dump() for s in sources],
                                    ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def paragraph_units(text: str) -> list[str]:
    """A Word paragraph can contain multiple clauses separated by manual line breaks."""
    return [part.strip() for part in re.split(
        r"(?m)(?=^[ \t\u3000\xa0]*第[零〇一二三四五六七八九十百千万\d]+[条章])", text)
        if part.strip()]


def split_paragraph(text: str, chunk_size: int) -> list[str]:
    """Prefer sentence ends without dropping characters; cap even a single huge sentence."""
    parts = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            boundaries = list(re.finditer(r'[。！？!?；;\n](?:[”’」』"])?|[.](?=\s)', text[start:end]))
            if boundaries:
                end = start + boundaries[-1].end()
        parts.append(text[start:end])
        start = end
    return parts


def parse_docx(path: Path, *, policy_key: str, version: str,
               effective_from: date, effective_to: date | None = None,
               chunk_size: int = 1400) -> PolicyBundle:
    """Preserve body order and table grid; never infer executable monetary rules."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    if chunk_size < 200 or chunk_size > 10000:
        raise ValueError("chunk_size must be between 200 and 10000")
    if path.suffix.lower() != ".docx" or path.is_symlink() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Expected a native DOCX file no larger than 20 MiB")
    with path.open("rb") as stream:
        raw = stream.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("DOCX exceeds the import limit")
    # Parse and hash the same immutable snapshot even if an editor changes the source concurrently.
    with ZipFile(BytesIO(raw)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError("DOCX expanded contents exceed the import limit")
    document = Document(BytesIO(raw))
    warnings = []
    if document.element.xpath(".//w:drawing"):
        warnings.append("文档含图片；图片内文字未识别，发布前须核对。")
    if document.element.xpath(".//w:ins | .//w:del | .//w:txbxContent"):
        raise ValueError("请先处理修订记录或文本框，再导入原生正文。")
    chunks = []
    parents = {}
    sections = {}
    section = "文档说明"
    section_id = "intro"
    clause = ""
    for index, block in enumerate(document.iter_inner_content()):
        block_id = f"b{index:04d}"
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            units = paragraph_units(text)
            for unit_index, unit in enumerate(units):
                parent = block_id if len(units) == 1 else f"{block_id}-u{unit_index}"
                style = block.style.name if block.style else ""
                if style.startswith("Heading") or re.match(r"^第[一二三四五六七八九十百]+章", unit):
                    section, clause = unit, ""
                    section_id = parent
                match = re.match(r"^(\d+(?:\.\d+)+)(?=\s)|^(第[零〇一二三四五六七八九十百千万\d]+条)", unit)
                if match:
                    clause = match.group(1) or match.group(2)
                for part, content in enumerate(split_paragraph(unit, chunk_size)):
                    ident = f"{parent}-{part}"
                    parents[ident], sections[ident] = parent, section_id
                    chunks.append(PolicyChunk(id=ident, section=section, clause=clause, text=content))
        elif isinstance(block, Table):
            if block._tbl.xpath(".//w:tc/w:tbl"):
                raise ValueError("检测到嵌套表格；请先展平并核对表头后重新导入。")
            rows = []
            for row in block.rows:
                rows.append([""] * row.grid_cols_before + [cell.text.strip() for cell in row.cells]
                            + [""] * row.grid_cols_after)
            if not rows or not any(any(row) for row in rows):
                continue
            if block._tbl.xpath(".//w:gridSpan | .//w:vMerge"):
                warnings.append(f"{block_id} 含合并单元格，已按网格展开；须核对表头对应关系。")
            if len(block._tbl.xpath("./w:tr/w:trPr/w:tblHeader")) > 1:
                warnings.append(f"{block_id} 标记了多行表头；当前按首行生成检索文本，发布前须核对。")
            # Repeated header rows are preserved, but complex multi-level headers need human review.
            headers = rows[0]
            if any(not value for value in headers) or len(set(headers)) != len(headers):
                warnings.append(f"{block_id} 表头存在空值或重复，发布前须人工核对。")
            for row_index, row in enumerate(rows[1:] or rows):
                ident = f"{block_id}-r{row_index + 1}"
                parents[ident], sections[ident] = block_id, section_id
                text = "；".join(f"{headers[i] if i < len(headers) and headers[i] else f'列{i + 1}'}：{value}"
                                for i, value in enumerate(row))
                chunks.append(PolicyChunk(
                    id=ident, section=section, clause=clause,
                    kind="table_row", text=f"{section}；条款{clause}；{text}",
                    table={"block_id": block_id, "row": row_index + 1, "headers": headers,
                           "cells": row, "grid": rows}))
    if not chunks:
        raise ValueError("Word contains no native text or tables")
    for chunk in chunks:
        explicit = set(re.findall(r"\b\d+(?:\.\d+)+\b|第[零〇一二三四五六七八九十百千万\d]+条", chunk.text))
        chunk.related_ids = [
            other.id for other in chunks if other.id != chunk.id and (
                (parents[other.id] == parents[chunk.id])
                or (chunk.clause and other.clause == chunk.clause
                    and sections[other.id] == sections[chunk.id])
                or (other.clause and other.clause != chunk.clause and other.clause in explicit)
                or (chunk.kind == "table_row" and sections[other.id] == sections[chunk.id]))]
    return PolicyBundle(policy_key=policy_key, version=version,
                        effective_from=effective_from, effective_to=effective_to,
                        source=path.name, source_sha256=hashlib.sha256(raw).hexdigest(),
                        chunks=chunks, warnings=list(dict.fromkeys(warnings)))
