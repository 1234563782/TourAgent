"""Offline assembly of a reviewed multi-source policy package."""

import hashlib
import json
from datetime import date
from pathlib import Path

from .policy_documents import PolicyBundle, PolicyChunk, PolicySource, parse_docx, sources_digest
from .policy_lodging import rows_from_snapshot
from .university_policy import UniversityRules


def prepare_package(manifest_path: Path, *, company_id: str) -> PolicyBundle:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.resolve().parent

    def local_file(name):
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Package files must exist inside the manifest directory")
        return path

    sources = []
    paths = {}
    for item in manifest["sources"]:
        path = local_file(item["path"])
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Package source exceeds 20 MiB")
        source = PolicySource(id=item["id"], role=item["role"], source=path.name,
                              sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        sources.append(source)
        paths[source.id] = path
    body = [source for source in sources if source.role == "body"]
    attachment = [source for source in sources if source.role == "lodging"]
    if len(sources) != 2 or len(body) != 1 or len(attachment) != 1:
        raise ValueError("Expected one body document and one lodging attachment")
    body, attachment = body[0], attachment[0]
    parsed = parse_docx(paths[body.id], company_id=company_id, policy_key=manifest["policy_key"],
                        version=manifest["version"], effective_from=date.fromisoformat(manifest["effective_from"]),
                        chunk_size=manifest["chunk_size"])
    if parsed.source_sha256 != body.sha256:
        raise ValueError("Body changed during package preparation")
    snapshot = json.loads(local_file(manifest["snapshot"]).read_text(encoding="utf-8-sig"))
    if snapshot["source_sha256"].lower() != attachment.sha256 or snapshot["source"] != attachment.source:
        raise ValueError("Attachment snapshot is not from the supplied workbook")
    rows = rows_from_snapshot(snapshot, manifest["layout"], attachment.id)
    rules = UniversityRules.model_validate_json(local_file(manifest["rules"]).read_text(encoding="utf-8"))
    clauses = {chunk.clause for chunk in parsed.chunks}
    if any(clause not in clauses for group in rules.source_clauses.values() for clause in group):
        raise ValueError("Rule references an absent clause")
    chunks = parsed.chunks
    # Import small evidence groups, not an entire chapter or every city through link closure.
    for chunk in chunks:
        chunk.table = {**(chunk.table or {}), "_provenance": {
            "source_id": body.id, "source": body.source, "sha256": body.sha256,
            "adoption_note": manifest["adoption_note"]}}
        chunk.related_ids = [other.id for other in chunks
                             if other.id != chunk.id and chunk.clause and other.clause == chunk.clause]
    for row in rows:
        text = (f"住宿费标准明细表；地区：{row.region}；适用地区：{'、'.join(row.localities)}；"
                "单位：元/人·天；" + "；".join(f"{grade}：{amount}" for grade, amount in row.caps.items()))
        if row.seasons:
            text += (f"；旺季地区：{'、'.join(row.seasonal_localities)}；旺季期间：{row.season_text}；"
                     + "；".join(f"旺季{grade}：{amount}" for grade, amount in row.seasonal_caps.items()))
        chunks.append(PolicyChunk(
            id=row.id, section="住宿费标准明细表", text=text, kind="lodging_row",
            table={"lodging_row": row.model_dump(mode="json"),
                   "_provenance": {"source_id": attachment.id, "source": attachment.source,
                                   "sha256": attachment.sha256, "sheet": row.sheet,
                                   "cell_range": row.cell_range, "adoption_note": manifest["adoption_note"]}},
            related_ids=[c.id for c in parsed.chunks if c.clause in manifest["attachment_clauses"]]))
    return PolicyBundle(
        schema_version=2, company_id=company_id, policy_key=manifest["policy_key"],
        version=manifest["version"], effective_from=manifest["effective_from"],
        effective_to=manifest.get("effective_to"),
        source=manifest["name"], source_sha256=sources_digest(sources), sources=sources,
        adoption_note=manifest["adoption_note"], chunks=chunks,
        warnings=parsed.warnings + manifest["warnings"],
        rules=rules.model_dump(mode="json"), lodging_rows=[row.model_dump(mode="json") for row in rows])
