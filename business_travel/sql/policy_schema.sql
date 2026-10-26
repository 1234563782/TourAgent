-- 政策存储第一版。数据库管理员须事先提供可安装 vector 扩展的 PostgreSQL。
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_documents (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id text NOT NULL,
    policy_key text NOT NULL,
    version text NOT NULL,
    effective_from date NOT NULL,
    effective_to date,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','published')),
    source text NOT NULL,
    source_sha256 text NOT NULL,
    bundle_sha256 text NOT NULL,
    embedding_space text NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimensions integer NOT NULL CHECK (embedding_dimensions > 0),
    tokenizer text NOT NULL,
    warnings jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    UNIQUE (company_id, policy_key, version),
    CHECK (effective_to IS NULL OR effective_to >= effective_from)
);
CREATE INDEX IF NOT EXISTS policy_documents_scope_idx
    ON policy_documents (company_id, policy_key, status, effective_from);

CREATE TABLE IF NOT EXISTS policy_chunks (
    document_id bigint NOT NULL REFERENCES policy_documents(id),
    chunk_id text NOT NULL,
    section text NOT NULL,
    clause text NOT NULL,
    content text NOT NULL,
    table_data jsonb,
    related_ids text[] NOT NULL DEFAULT '{}',
    embedding vector NOT NULL,
    embedding_dimensions integer NOT NULL,
    keywords tsvector NOT NULL,
    PRIMARY KEY (document_id, chunk_id),
    CHECK (vector_dims(embedding) = embedding_dimensions)
);
CREATE INDEX IF NOT EXISTS policy_chunks_keywords_idx ON policy_chunks USING gin (keywords);
-- 向量 HNSW 索引按运行时 Embedding 维度创建，见 PostgresPolicyStore.initialize/migrate_hnsw。
-- 不能在这里写死 vector(维度)，否则更换模型会把索引表达式与实际向量维度混用。

CREATE TABLE IF NOT EXISTS policy_rules (
    document_id bigint PRIMARY KEY REFERENCES policy_documents(id),
    rules jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_check_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES policy_documents(id),
    expense_kind text NOT NULL,
    expense_date date NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 同一发布版本绑定正文、附表及其结构化行；不跨文档复用住宿限额。
CREATE TABLE IF NOT EXISTS policy_sources (
    document_id bigint NOT NULL REFERENCES policy_documents(id),
    source_id text NOT NULL,
    source text NOT NULL,
    source_sha256 text NOT NULL,
    role text NOT NULL,
    adoption_note text NOT NULL,
    PRIMARY KEY (document_id, source_id)
);

CREATE TABLE IF NOT EXISTS policy_lodging_limits (
    document_id bigint NOT NULL,
    row_id text NOT NULL,
    source_id text NOT NULL,
    data jsonb NOT NULL,
    PRIMARY KEY (document_id, row_id),
    FOREIGN KEY (document_id, row_id) REFERENCES policy_chunks(document_id, chunk_id),
    FOREIGN KEY (document_id, source_id) REFERENCES policy_sources(document_id, source_id)
);
