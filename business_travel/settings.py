"""Deployment configuration is read from environment variables."""

import os
import json
import re
from io import StringIO
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream


def read_environment(env_file: Path) -> dict:
    values = {}
    if os.environ.get("TRAVEL_LOAD_DOTENV", "1") != "0":
        try:
            text = env_file.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError):
            raise ValueError("Cannot read project .env; check permissions and UTF-8 encoding") from None
        else:
            if any(binding.error for binding in parse_stream(StringIO(text))):
                raise ValueError("Invalid .env syntax; check assignments and quotes") from None
            values = {key: value for key, value in
                      dotenv_values(stream=StringIO(text), interpolate=False).items() if value is not None}
    return {**values, **os.environ}


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    base_url: str
    node_path: str
    flyai_cli_path: str
    storage_dir: Path
    legacy_memory_dir: Path
    policy_rules_path: Path | None = None
    policy_docs_dir: Path | None = None
    amap_key: str = ""
    weather_url: str = "https://wttr.in"
    amap_url: str = "https://restapi.amap.com"
    timeout: float = 45
    max_steps: int = 30
    policy_max_checks: int = 3
    slot_max_attempts: int = 3
    slot_context_limit: int = 16
    slot_task_limit: int = 4
    planner_max_attempts: int = 2
    context_window_tokens: int = 65536
    context_output_tokens: int = 8192
    context_margin_tokens: int = 4096
    context_frame_tokens: int = 1024
    context_recent_tokens: int = 12000
    context_summary_tokens: int = 2048
    context_tool_tokens: int = 2048
    context_read_tokens: int = 6000
    context_role_input_tokens: dict = field(default_factory=dict)
    model_extra_body: dict | None = field(default=None, repr=False)
    max_candidates: int = 10
    travel_provider: str = "tuniu"
    tuniu_flight_url: str = "https://openapi.tuniu.cn/hybrid/mcp/flight"
    tuniu_train_url: str = "https://openapi.tuniu.cn/hybrid/mcp/train"
    tuniu_hotel_url: str = "https://openapi.tuniu.cn/hybrid/mcp/hotel"
    tuniu_access_token: str = field(default="", repr=False)
    tuniu_flight_token: str = field(default="", repr=False)
    tuniu_train_token: str = field(default="", repr=False)
    tuniu_hotel_token: str = field(default="", repr=False)
    tuniu_auth_type: str = "oauth"
    tuniu_api_key: str = field(default="", repr=False)
    tuniu_hotel_detail_limit: int = 3
    tuniu_hotel_rate_limit: int = 4
    tuniu_hotel_detail_timeout: float = 12
    tuniu_hotel_detail_budget: float = 25
    policy_backend: str = "local"
    policy_database_url: str = field(default="", repr=False)
    policy_company_id: str = ""
    policy_key: str = "travel"
    embedding_base_url: str = ""
    embedding_api_key: str = field(default="", repr=False)
    embedding_model: str = ""
    embedding_dimensions: int = 0
    embedding_revision: str = "1"
    embedding_batch_size: int = 1
    policy_top_k: int = 5
    policy_recall_k: int = 20
    policy_rrf_k: int = 60
    policy_context_limit: int = 40
    policy_context_chars: int = 24000
    policy_rag_max_retrievals: int = 2
    policy_rag_max_answer_attempts: int = 2
    policy_dictionary_path: Path | None = None
    memory_database_url: str = field(default="", repr=False)
    memory_company_id: str = ""
    memory_schema: str = "travel_memory"
    memory_connect_timeout: int = 10
    memory_history_limit: int = 10
    resource_user_concurrency: int = 1
    resource_global_concurrency: int = 8
    resource_queue_size: int = 8
    resource_queue_timeout: float = 2.0
    resource_poll_seconds: float = 0.05
    resource_user_daily_requests: int = 100
    resource_company_daily_requests: int = 1000
    resource_user_daily_calls: int = 2000
    resource_company_daily_calls: int = 20000
    resource_request_calls: int = 24
    db_pool_size: int = 12
    db_session_pool_size: int = 36
    db_pool_waiting: int = 16
    db_pool_timeout: float = 5.0
    langfuse_enabled: bool = False
    langfuse_capture_content: bool = False
    langfuse_content_max_chars: int = 131072
    langfuse_base_url: str = field(default="", repr=False)
    langfuse_public_key: str = field(default="", repr=False)
    langfuse_secret_key: str = field(default="", repr=False)
    langfuse_hash_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env_file: Path | None = None):
        root = Path(__file__).resolve().parent.parent
        env = read_environment(env_file if env_file is not None else root / ".env")
        getenv = env.get
        optional = lambda name: Path(env[name]).resolve() if getenv(name) else None
        auth_type = getenv("TUNIU_AUTH_TYPE", "oauth").strip()
        if auth_type not in {"oauth", "apiKey"}:
            raise ValueError("TUNIU_AUTH_TYPE must be oauth or apiKey")
        prefix = "/mcp" if auth_type == "apiKey" else "/hybrid/mcp"
        extra_body = None
        if getenv("TRAVEL_LLM_EXTRA_BODY"):
            try:
                extra_body = json.loads(getenv("TRAVEL_LLM_EXTRA_BODY"))
            except (ValueError, TypeError):
                raise ValueError("TRAVEL_LLM_EXTRA_BODY must be a JSON object") from None
            if not isinstance(extra_body, dict):
                raise ValueError("TRAVEL_LLM_EXTRA_BODY must be a JSON object")
        try:
            role_budgets = json.loads(getenv("TRAVEL_CONTEXT_ROLE_INPUT_TOKENS") or "{}")
        except (ValueError, TypeError):
            raise ValueError("TRAVEL_CONTEXT_ROLE_INPUT_TOKENS must be a JSON object") from None
        settings = cls(
            api_key=getenv("TRAVEL_LLM_API_KEY", ""),
            model=getenv("TRAVEL_LLM_MODEL", ""),
            base_url=getenv("TRAVEL_LLM_BASE_URL", ""),
            node_path=getenv("FLYAI_NODE_PATH", ""),
            flyai_cli_path=getenv("FLYAI_CLI_PATH", ""),
            storage_dir=Path(getenv("TRAVEL_STORAGE_DIR", str(root / "data" / "graph"))),
            legacy_memory_dir=Path(getenv("TRAVEL_LEGACY_MEMORY_DIR", str(root / "data" / "memory"))),
            policy_rules_path=optional("TRAVEL_POLICY_RULES_PATH"),
            policy_docs_dir=optional("TRAVEL_POLICY_DOCS_DIR"),
            amap_key=getenv("AMAP_API_KEY", ""),
            weather_url=getenv("TRAVEL_WEATHER_URL", "https://wttr.in"),
            amap_url=getenv("TRAVEL_AMAP_URL", "https://restapi.amap.com"),
            timeout=float(getenv("TRAVEL_TIMEOUT_SECONDS", "45")),
            max_steps=int(getenv("TRAVEL_MAX_STEPS", "30")),
            policy_max_checks=int(getenv("TRAVEL_POLICY_MAX_CHECKS", "3")),
            slot_max_attempts=int(getenv("TRAVEL_SLOT_MAX_ATTEMPTS", "3")),
            slot_context_limit=int(getenv("TRAVEL_SLOT_CONTEXT_LIMIT", "16")),
            slot_task_limit=int(getenv("TRAVEL_SLOT_TASK_LIMIT", "4")),
            planner_max_attempts=int(getenv("TRAVEL_PLANNER_MAX_ATTEMPTS", "2")),
            context_window_tokens=int(getenv("TRAVEL_CONTEXT_WINDOW_TOKENS", "65536")),
            context_output_tokens=int(getenv("TRAVEL_CONTEXT_OUTPUT_TOKENS", "8192")),
            context_margin_tokens=int(getenv("TRAVEL_CONTEXT_MARGIN_TOKENS", "4096")),
            context_frame_tokens=int(getenv("TRAVEL_CONTEXT_FRAME_TOKENS", "1024")),
            context_recent_tokens=int(getenv("TRAVEL_CONTEXT_RECENT_TOKENS", "12000")),
            context_summary_tokens=int(getenv("TRAVEL_CONTEXT_SUMMARY_TOKENS", "2048")),
            context_tool_tokens=int(getenv("TRAVEL_CONTEXT_TOOL_TOKENS", "2048")),
            context_read_tokens=int(getenv("TRAVEL_CONTEXT_READ_TOKENS", "6000")),
            context_role_input_tokens=role_budgets,
            model_extra_body=extra_body,
            max_candidates=int(getenv("TRAVEL_MAX_CANDIDATES", "10")),
            travel_provider=getenv("TRAVEL_PROVIDER", "tuniu").strip(),
            tuniu_flight_url=getenv("TUNIU_FLIGHT_MCP_URL") or f"https://openapi.tuniu.cn{prefix}/flight",
            tuniu_train_url=getenv("TUNIU_TRAIN_MCP_URL") or f"https://openapi.tuniu.cn{prefix}/train",
            tuniu_hotel_url=getenv("TUNIU_HOTEL_MCP_URL") or f"https://openapi.tuniu.cn{prefix}/hotel",
            tuniu_access_token=getenv("TUNIU_ACCESS_TOKEN", "").strip(),
            tuniu_flight_token=getenv("TUNIU_FLIGHT_ACCESS_TOKEN", "").strip(),
            tuniu_train_token=getenv("TUNIU_TRAIN_ACCESS_TOKEN", "").strip(),
            tuniu_hotel_token=getenv("TUNIU_HOTEL_ACCESS_TOKEN", "").strip(),
            tuniu_auth_type=auth_type,
            tuniu_api_key=getenv("TUNIU_API_KEY", "").strip(),
            tuniu_hotel_detail_limit=int(getenv("TUNIU_HOTEL_DETAIL_LIMIT", "3")),
            tuniu_hotel_rate_limit=int(getenv("TUNIU_HOTEL_RATE_LIMIT", "4")),
            tuniu_hotel_detail_timeout=float(getenv("TUNIU_HOTEL_DETAIL_TIMEOUT", "12")),
            tuniu_hotel_detail_budget=float(getenv("TUNIU_HOTEL_DETAIL_BUDGET", "25")),
            policy_backend=getenv("TRAVEL_POLICY_BACKEND", "local").strip(),
            policy_database_url=getenv("TRAVEL_POLICY_DATABASE_URL", "").strip(),
            policy_company_id=getenv("TRAVEL_POLICY_COMPANY_ID", "").strip(),
            policy_key=getenv("TRAVEL_POLICY_KEY", "travel").strip(),
            embedding_base_url=getenv("TRAVEL_EMBEDDING_BASE_URL", "").strip(),
            embedding_api_key=getenv("TRAVEL_EMBEDDING_API_KEY", "").strip(),
            embedding_model=getenv("TRAVEL_EMBEDDING_MODEL", "").strip(),
            embedding_dimensions=int(getenv("TRAVEL_EMBEDDING_DIMENSIONS") or "0"),
            embedding_revision=getenv("TRAVEL_EMBEDDING_REVISION") or "1",
            embedding_batch_size=int(getenv("TRAVEL_EMBEDDING_BATCH_SIZE") or "1"),
            policy_top_k=int(getenv("TRAVEL_POLICY_TOP_K") or "5"),
            policy_recall_k=int(getenv("TRAVEL_POLICY_RECALL_K") or "20"),
            policy_rrf_k=int(getenv("TRAVEL_POLICY_RRF_K") or "60"),
            policy_context_limit=int(getenv("TRAVEL_POLICY_CONTEXT_LIMIT") or "40"),
            policy_context_chars=int(getenv("TRAVEL_POLICY_CONTEXT_CHARS") or "24000"),
            policy_rag_max_retrievals=int(getenv("TRAVEL_POLICY_RAG_MAX_RETRIEVALS") or "2"),
            policy_rag_max_answer_attempts=int(getenv("TRAVEL_POLICY_RAG_MAX_ANSWER_ATTEMPTS") or "2"),
            policy_dictionary_path=optional("TRAVEL_POLICY_DICTIONARY_PATH"),
            memory_database_url=(getenv("TRAVEL_MEMORY_DATABASE_URL", "").strip()
                                 or getenv("TRAVEL_POLICY_DATABASE_URL", "").strip()),
            memory_company_id=(getenv("TRAVEL_MEMORY_COMPANY_ID", "").strip()
                               or getenv("TRAVEL_POLICY_COMPANY_ID", "").strip()),
            memory_schema=getenv("TRAVEL_MEMORY_SCHEMA") or "travel_memory",
            memory_connect_timeout=int(getenv("TRAVEL_MEMORY_CONNECT_TIMEOUT") or "10"),
            memory_history_limit=int(getenv("TRAVEL_MEMORY_HISTORY_LIMIT") or "10"),
            **{name: cast(getenv("TRAVEL_" + name.upper(), str(default)))
               for name, (cast, default) in cls.resource_defaults().items()},
            langfuse_enabled=getenv("TRAVEL_LANGFUSE_ENABLED", "0").strip() == "1",
            langfuse_capture_content=getenv("TRAVEL_LANGFUSE_CAPTURE_CONTENT", "0").strip() == "1",
            langfuse_content_max_chars=int(getenv("TRAVEL_LANGFUSE_CONTENT_MAX_CHARS", "131072")),
            langfuse_base_url=getenv("LANGFUSE_BASE_URL", "").strip(),
            langfuse_public_key=getenv("LANGFUSE_PUBLIC_KEY", "").strip(),
            langfuse_secret_key=getenv("LANGFUSE_SECRET_KEY", "").strip(),
            langfuse_hash_key=getenv("TRAVEL_LANGFUSE_HASH_KEY", "").strip(),
        )
        if settings.timeout <= 0 or settings.max_steps < 4 or settings.max_candidates < 1:
            raise ValueError("Timeout, step limit and candidate limit must be positive")
        settings.validate_context()
        settings.validate_resources()
        if not 1 <= settings.policy_max_checks <= settings.max_steps:
            raise ValueError("TRAVEL_POLICY_MAX_CHECKS must be within the graph step limit")
        if not 1 <= settings.slot_max_attempts <= 5 or not 1 <= settings.planner_max_attempts <= 5:
            raise ValueError("Slot and planner attempts must be between 1 and 5")
        if not 2 <= settings.slot_context_limit <= 100 or not 1 <= settings.slot_task_limit <= 20:
            raise ValueError("Invalid slot context or task limit")
        if settings.slot_task_limit * 4 + 2 > settings.max_steps:
            raise ValueError("Task limit requires a larger TRAVEL_MAX_STEPS")
        if settings.travel_provider not in {"tuniu", "flyai"}:
            raise ValueError("TRAVEL_PROVIDER must be tuniu or flyai")
        if (not 0 <= settings.tuniu_hotel_detail_limit <= 10
                or not 1 <= settings.tuniu_hotel_rate_limit <= 20
                or not 0 < settings.tuniu_hotel_detail_timeout <= 120
                or not 0 < settings.tuniu_hotel_detail_budget <= 120):
            raise ValueError("Invalid Tuniu hotel detail limits or deadlines")
        if settings.policy_backend not in {"local", "pgvector"}:
            raise ValueError("TRAVEL_POLICY_BACKEND must be local or pgvector")
        if (not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", settings.memory_schema)
                or settings.memory_schema == "public" or settings.memory_schema.startswith("pg_")):
            raise ValueError("TRAVEL_MEMORY_SCHEMA must be a dedicated lowercase SQL identifier")
        if settings.memory_company_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", settings.memory_company_id):
            raise ValueError("TRAVEL_MEMORY_COMPANY_ID must contain 1-64 ASCII ID characters")
        if (settings.memory_company_id and settings.policy_company_id
                and settings.memory_company_id != settings.policy_company_id):
            raise ValueError("Memory and policy company IDs must match for this deployment")
        if not 1 <= settings.memory_connect_timeout <= 120 or not 1 <= settings.memory_history_limit <= 100:
            raise ValueError("Invalid memory connection timeout or history limit")
        if not 1 <= settings.embedding_batch_size <= 64:
            raise ValueError("TRAVEL_EMBEDDING_BATCH_SIZE must be between 1 and 64")
        if not (1 <= settings.policy_top_k <= settings.policy_recall_k <= 100
                and settings.policy_rrf_k > 0
                and settings.policy_top_k <= settings.policy_context_limit <= 200
                and 0 <= settings.embedding_dimensions <= 16000):
            raise ValueError("Invalid policy retrieval limits or embedding dimensions")
        if (not 1000 <= settings.policy_context_chars <= 100000
                or not 1 <= settings.policy_rag_max_retrievals <= 3
                or not 1 <= settings.policy_rag_max_answer_attempts <= 3):
            raise ValueError("Invalid policy evidence budget or retrieval attempts")
        return settings

    @classmethod
    def resource_defaults(cls):
        names = [name for name in cls.__dataclass_fields__
                 if name.startswith(("resource_", "db_pool_", "db_session_pool_"))]
        return {name: (type(cls.__dataclass_fields__[name].default),
                       cls.__dataclass_fields__[name].default) for name in names}

    def validate_resources(self):
        import math
        for name, (cast, _) in self.resource_defaults().items():
            value = getattr(self, name)
            minimum = 0 if name == "resource_queue_size" else 0.001
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < minimum
                    or (cast is int and not isinstance(value, int))):
                raise ValueError("Invalid resource setting: " + name)
        if self.resource_user_concurrency > self.resource_global_concurrency:
            raise ValueError("User concurrency cannot exceed global concurrency")
        if self.resource_poll_seconds > self.resource_queue_timeout:
            raise ValueError("Resource polling interval exceeds queue timeout")
        if self.resource_request_calls > min(self.resource_user_daily_calls,
                                             self.resource_company_daily_calls):
            raise ValueError("Daily call quota must cover one request reservation")
        if self.db_session_pool_size < 2 * (self.resource_global_concurrency + self.resource_queue_size) + 2:
            raise ValueError("Session pool must cover executors, queue and read headroom")

    def validate_context(self):
        sizes = (self.context_window_tokens, self.context_output_tokens, self.context_margin_tokens,
                 self.context_recent_tokens, self.context_summary_tokens, self.context_tool_tokens,
                 self.context_read_tokens)
        if any(type(value) is not int or value < 256 for value in sizes):
            raise ValueError("Context budgets must be integers of at least 256")
        available = self.context_window_tokens - self.context_output_tokens - self.context_margin_tokens
        if available < 1024 or any(value > available for value in sizes[3:]):
            raise ValueError("Context window must cover input, output and safety budgets")
        if (type(self.context_frame_tokens) is not int
                or not 0 <= self.context_frame_tokens < available):
            raise ValueError("TRAVEL_CONTEXT_FRAME_TOKENS must be a nonnegative integer below the input budget")
        roles = {"supervisor", "slots", "reviewer", "planner", "policy", "policy_reviewer"}
        if (not isinstance(self.context_role_input_tokens, dict)
                or any(role not in roles or type(value) is not int or not 1024 <= value <= available
                       for role, value in self.context_role_input_tokens.items())):
            raise ValueError("Invalid context role input budgets")

    def missing_memory_config(self) -> list[str]:
        return [name for name, value in (
            ("TRAVEL_MEMORY_DATABASE_URL", self.memory_database_url),
            ("TRAVEL_MEMORY_COMPANY_ID", self.memory_company_id),
        ) if not value]

    def missing_policy_config(self, *, embedding=True) -> list[str]:
        values = [
            ("TRAVEL_POLICY_DATABASE_URL", self.policy_database_url),
            ("TRAVEL_POLICY_COMPANY_ID", self.policy_company_id),
            ("TRAVEL_POLICY_KEY", self.policy_key),
        ]
        if embedding:
            values.extend([
                ("TRAVEL_EMBEDDING_BASE_URL", self.embedding_base_url),
                ("TRAVEL_EMBEDDING_API_KEY", self.embedding_api_key),
                ("TRAVEL_EMBEDDING_MODEL", self.embedding_model),
                ("TRAVEL_EMBEDDING_DIMENSIONS", self.embedding_dimensions),
            ])
        return [name for name, value in values if not value]

    def tuniu_token(self, kind: str) -> str:
        if kind not in {"flight", "train", "hotel"}:
            raise ValueError("Unsupported Tuniu service")
        return getattr(self, f"tuniu_{kind}_token") or self.tuniu_access_token

    def tuniu_credential(self, kind: str) -> str:
        if self.tuniu_auth_type == "apiKey":
            return self.tuniu_api_key
        return self.tuniu_token(kind)

    def missing_model_config(self) -> list[str]:
        return [name for name, value in (
            ("TRAVEL_LLM_API_KEY", self.api_key),
            ("TRAVEL_LLM_MODEL", self.model),
            ("TRAVEL_LLM_BASE_URL", self.base_url),
        ) if not value.strip()]
