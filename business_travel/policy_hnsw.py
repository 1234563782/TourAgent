"""Validated HNSW DDL helpers for the policy vector store."""


def hnsw_index_name(dimensions: int) -> str:
    _validate_dimensions(dimensions)
    return f"policy_chunks_embedding_hnsw_{dimensions}"


def _validate_dimensions(dimensions: int) -> None:
    if type(dimensions) is not int or not 1 <= dimensions <= 2000:
        raise ValueError("embedding_dimensions must be between 1 and 2000 for HNSW")


def validate_hnsw_parameters(dimensions: int, *, m: int, ef_construction: int,
                             ef_search: int) -> None:
    _validate_dimensions(dimensions)
    if type(m) is not int or not 4 <= m <= 100:
        raise ValueError("HNSW m must be between 4 and 100")
    if type(ef_construction) is not int or not m <= ef_construction <= 1000:
        raise ValueError("HNSW ef_construction must be between m and 1000")
    if type(ef_search) is not int or not 1 <= ef_search <= 10000:
        raise ValueError("HNSW ef_search must be between 1 and 10000")


def render_index_ddl(dimensions: int, *, m: int = 16, ef_construction: int = 128) -> str:
    """Render a global cosine HNSW index for the configured vector dimension."""
    validate_hnsw_parameters(dimensions, m=m, ef_construction=ef_construction, ef_search=1)
    name = hnsw_index_name(dimensions)
    return (
        f"CREATE INDEX IF NOT EXISTS {name} ON policy_chunks "
        f"USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops) "
        f"WITH (m = {m}, ef_construction = {ef_construction});"
    )
