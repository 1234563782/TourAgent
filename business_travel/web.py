"""Same-origin login UI and authenticated personal memory management."""

from contextlib import asynccontextmanager, contextmanager, ExitStack
from hashlib import sha256
import logging
from pathlib import Path
from threading import BoundedSemaphore
from urllib.parse import urlsplit

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import AuthSettings, AuthStore, COOKIE_NAME, LoginLimited, username_value
from .memory_pg import MemoryIdentity, PostgresTravelMemory
from .memory_pg import open_checkpointer, memory_connection
from .graph import build_assistant
from .models import create_model, model_error_details, safe_model_error_message
from .policy import PolicyStore
from .providers import InformationProvider, FlyAIProvider
from .tuniu import TuniuProvider
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from .settings import Settings
from .observability import Telemetry
from .conversations import ConversationStore
from .execution_events import progress_event, public_graph_events
from .streaming import ChatEventResponse
from .chat_runs import (ChatRunStore, FINISHED_STATUSES, REVIEW_STATUSES,
                        public_run, request_digest)
from .run_recovery import RecoveryBody, RecoveryConflict, RecoveryUnavailable, recover_run
from .resources import (execution_resources, AdmissionRejected, CallBudgetExceeded,
                        RunDeadlineExceeded)
from .db_pool import close_pools
from .invoice_contracts import InvoiceBatch
from .attachment_contracts import AttachmentUpload
from .attachment_store import AttachmentInputError, PostgresAttachmentStore, inspect_upload
from .invoice_review import InvoicePolicy, review_upload
from .invoice_store import PostgresInvoiceStore

STATIC = Path(__file__).resolve().parent / "web_static"
LOG = logging.getLogger(__name__)


class BodyLimitMiddleware:
    """Bound JSON buffering before FastAPI validation, including chunked uploads."""

    def __init__(self, app, invoice_limit=4096, attachment_limit=4096):
        self.app = app
        self.invoice_limit = invoice_limit
        self.attachment_limit = attachment_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body = event.get("body", b"")
            size += len(body)
            chat_path = scope["path"] in {"/api/chat", "/api/chat/stream"} or (
                scope["path"].startswith("/api/conversations/")
                and scope["path"].endswith(("/chat", "/chat/stream")))
            limit = 20000 if chat_path else 4096
            if scope["path"] == "/api/invoices/review":
                limit = self.invoice_limit
            if scope["path"] == "/api/attachments/inspect":
                limit = self.attachment_limit
            if size > limit:
                return await JSONResponse({"detail": "请求内容过大"}, status_code=413)(scope, receive, send)
            chunks.append(body)
            if not event.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def valid_username(cls, value):
        return username_value(value)

    @field_validator("password")
    @classmethod
    def valid_password(cls, value):
        if not 1 <= len(value.get_secret_value()) <= 128:
            raise ValueError("Invalid password length")
        return value


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    message: str | None = Field(default=None, min_length=1, max_length=3000)
    resume: StrictBool | None = None
    revision: str | None = Field(default=None, min_length=1, max_length=200)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("attachment_ids")
    @classmethod
    def valid_attachment_ids(cls, value):
        import re
        if len(set(value)) != len(value) or any(not re.fullmatch(r"[0-9a-f]{32}", item) for item in value):
            raise ValueError("Invalid attachment IDs")
        return value

    @model_validator(mode="after")
    def valid_action(self):
        if (self.message is None) == (self.resume is None):
            raise ValueError("One action required")
        if self.message is not None:
            self.message = self.message.strip()
            if not self.message or self.revision is not None:
                raise ValueError("Invalid message")
        elif self.revision is None:
            raise ValueError("Confirmation revision required")
        if self.resume is not None and self.attachment_ids:
            raise ValueError("Confirmation cannot include attachments")
        return self


@contextmanager
def conversation_lock(settings, identity, conversation_id="web"):
    # Session locks span model calls and work across multiple web workers.
    # The schema is part of the key: advisory locks are database-wide.
    key = int.from_bytes(sha256(
        f"travel_conversation:{settings.memory_schema}:{identity.thread_id(conversation_id)}".encode()
    ).digest()[:8], signed=True)
    with memory_connection(settings, lane="session") as connection:
        acquired = connection.execute(
            "SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()["acquired"]
        if not acquired:
            raise HTTPException(409, "此会话正在处理另一条请求，请稍后刷新")
        try:
            yield connection
        finally:
            try:
                connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
            except Exception:
                # A failed unlock must not mask the execution result. The pool
                # will discard the broken session on return; PostgreSQL releases
                # its advisory locks when that session closes.
                LOG.warning("Unable to release conversation lock; discarding connection",
                            exc_info=True)
                try:
                    connection.close()
                except Exception:
                    LOG.debug("Unable to close failed conversation connection", exc_info=True)


def chat_snapshot(state):
    """Expose user turns and final text, never internal tool messages."""
    messages = []
    answer = None
    for item in state.values.get("messages", []):
        if isinstance(item, HumanMessage):
            if answer is not None:
                messages.append({"role": "assistant", "text": answer})
            answer = None
            entry = {"role": "user", "text": item.content}
            attachments = [{key: attachment[key] for key in (
                "attachment_id", "filename", "attachment_kind", "attachment_status") if key in attachment}
                for attachment in (item.additional_kwargs or {}).get("attachments", [])
                if isinstance(attachment, dict)]
            if attachments:
                entry["attachments"] = attachments
            messages.append(entry)
        elif isinstance(item, AIMessage) and not item.tool_calls and isinstance(item.content, str):
            if item.content:
                answer = item.content
    if answer is not None:
        messages.append({"role": "assistant", "text": answer})
    pending = [entry.value for entry in state.interrupts]
    from .tasks import task_snapshot
    return {"messages": messages, "interrupt": pending or None,
            "tasks": task_snapshot(state.values.get("tasks", {})),
            "revision": (state.config or {}).get("configurable", {}).get("checkpoint_id")}


def create_app(settings=None, auth_config=None, store=None, *, invoice_store_factory=None,
               attachment_store_factory=None):
    settings = settings or Settings.from_env()
    auth_config = auth_config or AuthSettings.from_env()
    store = store or AuthStore(settings, auth_config)
    telemetry = Telemetry(settings)
    settings.validate_resources()
    settings.validate_invoices()
    invoice_store_factory = invoice_store_factory or PostgresInvoiceStore
    attachment_store_factory = attachment_store_factory or PostgresAttachmentStore
    # This is only a per-process backpressure gate. PostgreSQL remains the
    # cross-process source of truth for the actual execution slots.
    local_request_gate = BoundedSemaphore(
        settings.resource_global_concurrency + settings.resource_queue_size)

    @asynccontextmanager
    async def lifespan(app):
        try:
            store.check()
        except (psycopg.Error, ValueError, OSError):
            raise RuntimeError("鉴权存储不可用；检查数据库配置并运行 scripts/auth_admin.py init") from None
        try:
            with memory_connection(settings) as connection:
                connection.execute("SELECT 1 FROM resource_daily_usage LIMIT 0")
        except Exception:
            close_pools()
            raise RuntimeError("资源配额存储不可用；检查数据库并运行 scripts/auth_admin.py init-resources") from None
        try:
            yield
        finally:
            from starlette.concurrency import run_in_threadpool
            await run_in_threadpool(telemetry.shutdown)
            await run_in_threadpool(close_pools)

    app = FastAPI(title="高校差旅账号服务", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware, invoice_limit=settings.invoice_request_bytes,
                       attachment_limit=settings.attachment_request_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(auth_config.origin).hostname])

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.headers.get("origin") != auth_config.origin
            or request.headers.get("sec-fetch-site") == "cross-site"
        ):
            response = JSONResponse({"detail": "请求来源不允许"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if auth_config.secure_cookie:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Default validation responses may echo raw password inputs.
        return JSONResponse({"detail": "输入格式不正确"}, status_code=422)

    @app.exception_handler(psycopg.Error)
    async def storage_error(request, error):
        return JSONResponse({"detail": "服务暂不可用，请稍后再试"}, status_code=503)

    def current_user(request: Request):
        user = store.current_user(request.cookies.get(COOKIE_NAME))
        if user is None:
            raise HTTPException(401, "请先登录")
        return user

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request):
        try:
            token = store.login(body.username, body.password.get_secret_value(),
                                request.client.host if request.client else "unknown")
        except LoginLimited:
            raise HTTPException(429, "登录尝试过多，请稍后再试",
                                headers={"Retry-After": str(auth_config.rate_seconds)}) from None
        if token is None:
            raise HTTPException(401, "账号或密码错误，或账号暂不可用")
        # A successful login replaces and revokes this browser's previous session.
        store.logout(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE_NAME, token, max_age=auth_config.session_seconds,
                            httponly=True, secure=auth_config.secure_cookie, samesite="strict", path="/")
        return response

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request):
        store.logout(request.cookies.get(COOKIE_NAME))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, httponly=True, secure=auth_config.secure_cookie,
                               samesite="strict", path="/")
        return response

    @app.get("/api/auth/me")
    def me(user=Depends(current_user)):
        return {key: user[key] for key in ("user_id", "username")}

    @app.get("/api/memory")
    def memory(request: Request, user=Depends(current_user)):
        if request.query_params:
            raise HTTPException(400, "此接口不接受身份或查询参数")
        identity = MemoryIdentity(user["user_id"])
        return PostgresTravelMemory(settings, identity).read()

    @app.get("/api/invoices/config")
    def invoice_config(user=Depends(current_user)):
        return {"max_file_bytes": settings.invoice_max_file_bytes,
                "max_batch_files": settings.invoice_max_batch_files,
                "supported": ["UTF-8 EInvoice 0.32 XML / digital VAT"],
                "buyer_configured": bool(settings.invoice_buyer_name and settings.invoice_buyer_tax_id)}

    @app.post("/api/invoices/review")
    def invoice_review(body: InvoiceBatch, request: Request, user=Depends(current_user)):
        if request.query_params:
            raise HTTPException(400, "此接口不接受身份或查询参数")
        if len(body.files) > settings.invoice_max_batch_files:
            raise HTTPException(413, "文件数量超过批次上限，请分批提交")
        identity = MemoryIdentity(user["user_id"])
        from .personnel import PersonnelProfile
        profile = store.get_personnel(identity.user_id,) or PersonnelProfile()
        policy = InvoicePolicy(settings)
        registry = invoice_store_factory(settings, identity)
        results = [review_upload(upload, settings=settings, registry=registry,
                                 policy=policy, profile=profile).model_dump(mode="json")
                   for upload in body.files]
        return {"results": results, "verification_status": "not_checked", "approved": False}

    @app.get("/api/attachments/config")
    def attachment_config(user=Depends(current_user)):
        return {"max_file_bytes": settings.attachment_max_file_bytes,
                "max_files": settings.attachment_max_files,
                "supported": ["UTF-8 EInvoice 0.32 XML / digital VAT"],
                "notice": "附件仅保存并解析，尚未自动用于聊天费用核对；请用发票预审入口核对票面。"}

    @app.post("/api/attachments/inspect")
    def attachment_inspect(body: AttachmentUpload, request: Request, user=Depends(current_user)):
        if request.query_params:
            raise HTTPException(400, "此接口不接受身份或查询参数")
        identity = MemoryIdentity(user["user_id"])
        try:
            result = inspect_upload(body, settings=settings,
                                    store=attachment_store_factory(settings, identity))
        except AttachmentInputError as error:
            raise HTTPException(422, str(error)) from None
        except Exception:
            raise HTTPException(503, "附件保存失败；请管理员检查存储并运行 init-attachments 后重试。") from None
        return result.model_dump(mode="json")

    @app.get("/api/attachments/{attachment_id}")
    def attachment_detail(attachment_id: str, request: Request, user=Depends(current_user)):
        if request.query_params:
            raise HTTPException(400, "此接口不接受身份或查询参数")
        identity = MemoryIdentity(user["user_id"])
        try:
            return attachment_store_factory(settings, identity).get(attachment_id).model_dump(mode="json")
        except LookupError:
            raise HTTPException(404, "附件不存在或无权访问") from None
        except Exception:
            raise HTTPException(503, "附件记录暂不可用，请联系管理员检查存储。") from None

    def conversation_store(user):
        return ConversationStore(settings, MemoryIdentity(user["user_id"]))

    @contextmanager
    def local_request_slot(body):
        # GET/history requests do not enter the expensive execution path.
        if body is None:
            yield
            return
        if not local_request_gate.acquire(blocking=False):
            raise AdmissionRejected("local_backpressure", settings.resource_queue_timeout)
        try:
            yield
        finally:
            local_request_gate.release()

    @app.get("/api/conversations")
    def conversations(request: Request, offset: int = Query(default=0, ge=0),
                      user=Depends(current_user)):
        if set(request.query_params) - {"offset"}:
            raise HTTPException(400, "此接口不接受身份或其他查询参数")
        return conversation_store(user).list(offset=offset)

    @app.post("/api/conversations", status_code=201)
    def new_conversation(request: Request, user=Depends(current_user)):
        if request.query_params:
            raise HTTPException(400, "此接口不接受查询参数")
        return conversation_store(user).create()

    def owned_conversation(user, conversation_id):
        registry = conversation_store(user)
        try:
            return registry, registry.require(conversation_id)
        except LookupError:
            raise HTTPException(404, "对话不存在或无权访问") from None

    def assistant_graph(identity, checkpointer):
        provider = TuniuProvider(settings) if settings.travel_provider == "tuniu" else FlyAIProvider(settings)
        return build_assistant(lambda role: create_model(settings), settings,
                               PostgresTravelMemory(settings, identity), PolicyStore.from_settings(settings),
                               checkpointer=checkpointer, provider=provider,
                               information=InformationProvider(settings),
                               personnel_profile=store.get_personnel(identity.user_id,))

    def recovery_action(request, user, conversation_id, body=None):
        if request.query_params:
            raise HTTPException(400, "此接口不接受查询参数")
        owned_conversation(user, conversation_id)
        identity = MemoryIdentity(user["user_id"])
        try:
            with conversation_lock(settings, identity, conversation_id):
                ChatRunStore(settings, identity, conversation_id).interrupt_orphans()
                return recover_run(settings, identity, conversation_id,
                                   lambda saver: assistant_graph(identity, saver), body)
        except RecoveryConflict as error:
            raise HTTPException(409, str(error)) from None
        except RecoveryUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "恢复服务暂不可用，请刷新核实；不会重试原执行") from None

    @app.get("/api/conversations/{conversation_id}/recovery")
    def review_recovery(conversation_id: str, request: Request, user=Depends(current_user)):
        return recovery_action(request, user, conversation_id)

    @app.post("/api/conversations/{conversation_id}/recovery")
    def confirm_recovery(conversation_id: str, body: RecoveryBody, request: Request,
                         user=Depends(current_user)):
        return recovery_action(request, user, conversation_id, body)

    def read_run_status(request, user, conversation_id, request_id=None):
        if request.query_params:
            raise HTTPException(400, "此接口不接受查询参数")
        owned_conversation(user, conversation_id)
        identity = MemoryIdentity(user["user_id"])
        runs = ChatRunStore(settings, identity, conversation_id)
        # Acquiring the same lock proves no current executor owns these rows.
        try:
            with conversation_lock(settings, identity, conversation_id):
                runs.interrupt_orphans()
        except HTTPException as error:
            if error.status_code != 409:
                raise
        row = runs.get(request_id) if request_id else runs.latest()
        if request_id and row is None:
            raise HTTPException(404, "运行不存在或无权访问")
        return {"run": public_run(row)}

    @app.get("/api/conversations/{conversation_id}/runs")
    def latest_run(conversation_id: str, request: Request, user=Depends(current_user)):
        return read_run_status(request, user, conversation_id)

    @app.get("/api/conversations/{conversation_id}/runs/{request_id}")
    def run_status(conversation_id: str, request_id: str, request: Request,
                   user=Depends(current_user)):
        import re
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise HTTPException(404, "运行不存在或无权访问")
        return read_run_status(request, user, conversation_id, request_id)

    def run_chat(request, user, body=None, conversation_id=None, emit=None):
        if request.query_params:
            raise HTTPException(400, "此接口不接受身份或查询参数")
        identity = MemoryIdentity(user["user_id"])
        ident = conversation_id or "web"
        registry, metadata = owned_conversation(user, ident)
        attachments = None
        attachment_metadata = []
        if body is not None and body.attachment_ids:
            if len(body.attachment_ids) > settings.attachment_max_files:
                raise HTTPException(422, "附件数量超过当前上限，请分批发送")
            try:
                attachments = attachment_store_factory(settings, identity)
                attachment_metadata = attachments.load_for_chat(body.attachment_ids)
            except LookupError:
                raise HTTPException(404, "附件不存在或无权访问") from None
            except Exception:
                raise HTTPException(503, "附件记录暂不可用，请联系管理员检查存储。") from None
        runs = ChatRunStore(settings, identity, ident)
        config = {"configurable": {"thread_id": identity.thread_id(ident)},
                  "recursion_limit": 30}
        try:
            with local_request_slot(body), ExitStack() as execution:
                lock_connection = execution.enter_context(conversation_lock(settings, identity, ident))
                # Reuse the conversation-lock session for checkpoint access. A
                # waiting request therefore does not consume a second long-lived
                # connection before it reaches admission.
                checkpointer = execution.enter_context(
                    open_checkpointer(settings, identity, connection=lock_connection))
                latest = runs.latest()
                graph = assistant_graph(identity, checkpointer)
                existing = runs.get(body.request_id) if body else None
                if existing:
                    if existing["request_hash"] != request_digest(body):
                        raise HTTPException(409, "请求 ID 已用于其他内容，请勿修改后重复使用")
                    if existing["status"] not in FINISHED_STATUSES:
                        raise HTTPException(409, "此请求未能确认完成，请核实个人记录后开启新对话")
                    pinned = {"configurable": {**config["configurable"],
                                               "checkpoint_id": existing["checkpoint_id"]}}
                    current = chat_snapshot(graph.get_state(pinned))
                    if current["revision"] != existing["checkpoint_id"]:
                        raise HTTPException(503, "原运行结果不可用，请核实记录，不要重复提交")
                    return {**current, "conversation_id": ident, "run": public_run(existing)}
                if latest and latest["status"] in REVIEW_STATUSES:
                    if body:
                        raise HTTPException(409, "此会话有未核实的执行，请核实个人记录后开启新对话")
                    # Partial model output is not a committed answer. Show the prior checkpoint.
                    if latest["base_checkpoint_id"]:
                        pinned = {"configurable": {**config["configurable"],
                            "checkpoint_id": latest["base_checkpoint_id"]}}
                        current = chat_snapshot(graph.get_state(pinned))
                    else:
                        current = {"messages": [], "interrupt": None, "tasks": [], "revision": None}
                else:
                    current = chat_snapshot(graph.get_state(config))
                if body is not None:
                    if body.resume is not None:
                        if not current["interrupt"] or current["revision"] != body.revision:
                            raise HTTPException(409, "确认已失效，请刷新会话后重新核对")
                        action = Command(resume=body.resume)
                    else:
                        if current["interrupt"]:
                            raise HTTPException(409, "请先确认或取消当前待处理事项")
                        action = {"messages": [HumanMessage(content=body.message,
                            additional_kwargs={"attachments": attachment_metadata})]}
                    budget = execution.enter_context(execution_resources(settings, identity, lock_connection))
                    # Rejected requests must not touch history: only an admitted run
                    # may retire rows orphaned by a crashed executor.
                    runs.interrupt_orphans()
                    runs.start(body.request_id, request_digest(body), current["revision"])
                    with telemetry.request(config, identity, ident, body.request_id) as (trace, traced_config):
                        try:
                            prior_messages = list(current["messages"])
                            trace.content(input={"message": body.message, "resume": body.resume})
                            if attachments is not None:
                                attachments.link_message(ident, body.request_id, body.attachment_ids)
                            registry.touch(ident, body.message)
                            if emit:
                                emit("progress", {**progress_event("accepted"), "run_id": body.request_id})
                            for kind, data in public_graph_events(graph.stream(
                                    action, traced_config, stream_mode=["custom", "updates"])):
                                if kind == "progress":
                                    runs.progress(body.request_id, data["stage"])
                                if emit:
                                    emit(kind, {**data, "run_id": body.request_id})
                            # A graph can spend time in a non-model tool/provider
                            # step. Do not commit a completed checkpoint after the
                            # request deadline even when no further model call is made.
                            budget.check_deadline()
                            if budget.exhausted:
                                raise CallBudgetExceeded("request_model_call_quota_exceeded")
                            current = chat_snapshot(graph.get_state(config))
                            latest = runs.finish(body.request_id,
                                "awaiting_confirmation" if current["interrupt"] else "completed",
                                current["revision"])
                            trace.update(execution_status=latest["status"], checkpoint_committed=True)
                            # Export only newly visible messages, never a previous turn's answer.
                            prefix = 0
                            for before, after in zip(prior_messages, current["messages"]):
                                if before != after:
                                    break
                                prefix += 1
                            trace.content(output=lambda: {
                                "messages": [message for message in current["messages"][len(prior_messages):]
                                             if message["role"] == "assistant"],
                                "interrupt": current["interrupt"],
                                "status": latest["status"],
                            } if prefix == len(prior_messages) else {
                                "_omitted": "visible history changed; cannot isolate current answer",
                                "status": latest["status"],
                            })
                        except Exception:
                            # The checkpoint may already contain a write: never automatically replay.
                            runs.finish(body.request_id, "failed")
                            raise
                result = {**current, "conversation_id": ident, "run": public_run(latest)}
                if registry and body is None:
                    result["conversation"] = metadata
                return result
        except AdmissionRejected as error:
            messages = {
                "user_concurrency": "您的其他会话正在执行，请稍后再发送。本次尚未执行。",
                "queue_full": "当前请求过多，等待队列已满。本次尚未执行，请稍后再试。",
                "queue_timeout": "等待执行超时。本次尚未执行，请稍后再试。",
                "local_backpressure": "当前服务入口繁忙，本次尚未执行，请稍后再试。",
                "daily_quota": "今日请求或模型调用配额不足。本次尚未执行，请等待配额重置或联系管理员。",
            }
            raise HTTPException(429, messages[error.reason], headers={
                "Retry-After": str(error.retry_after), "X-Travel-Admission": "rejected",
            }) from None
        except RunDeadlineExceeded:
            raise HTTPException(503, "本轮执行超过时长上限，请刷新核实执行记录后使用恢复入口") from None
        except CallBudgetExceeded:
            raise HTTPException(503, "本轮模型调用已达上限，请刷新核实执行记录后使用恢复入口") from None
        except HTTPException:
            raise
        except Exception as error:
            # Provider exceptions can contain credential-bearing URLs; expose only
            # the allow-listed category and preserve the recovery boundary.
            details = model_error_details(error, stage="chat_execution")
            raise HTTPException(
                503,
                safe_model_error_message(details)
                + "请刷新核实会话结果后，再决定是否恢复或开启新对话。",
            ) from None

    @app.get("/api/chat")
    def chat_history(request: Request, user=Depends(current_user)):
        return run_chat(request, user)

    @app.post("/api/chat")
    def chat(body: ChatBody, request: Request, user=Depends(current_user)):
        return run_chat(request, user, body)

    @app.post("/api/chat/stream")
    def chat_stream(body: ChatBody, request: Request, user=Depends(current_user)):
        return ChatEventResponse(lambda emit: run_chat(request, user, body, emit=emit))

    @app.get("/api/conversations/{conversation_id}/chat")
    def conversation_history(conversation_id: str, request: Request, user=Depends(current_user)):
        return run_chat(request, user, conversation_id=conversation_id)

    @app.post("/api/conversations/{conversation_id}/chat")
    def conversation_chat(conversation_id: str, body: ChatBody, request: Request,
                          user=Depends(current_user)):
        return run_chat(request, user, body, conversation_id)

    @app.post("/api/conversations/{conversation_id}/chat/stream")
    def conversation_stream(conversation_id: str, body: ChatBody, request: Request,
                            user=Depends(current_user)):
        return ChatEventResponse(lambda emit: run_chat(request, user, body, conversation_id, emit))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
