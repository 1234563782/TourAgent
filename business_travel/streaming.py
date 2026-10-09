"""Bounded SSE delivery with one worker owning the entire synchronous operation."""

import json
from threading import Event

import anyio
from fastapi import HTTPException
from starlette.responses import JSONResponse, Response, StreamingResponse

from .models import model_error_details, safe_model_error_message


class ChatEventResponse(Response):
    def __init__(self, producer, *, buffer_size=16):
        super().__init__(media_type="text/event-stream")
        self.producer = producer
        self.buffer_size = buffer_size
        del self.headers["content-length"]

    async def __call__(self, scope, receive, send):
        sender, receiver = anyio.create_memory_object_stream(self.buffer_size)
        disconnected = Event()
        cancelled = anyio.get_cancelled_exc_class()

        def emit(kind, data):
            if disconnected.is_set():
                return
            try:
                anyio.from_thread.run(sender.send, (kind, data))
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, cancelled):
                disconnected.set()

        def execute():
            try:
                result = self.producer(emit)
            except HTTPException as error:
                emit("error", {"status": error.status_code, "detail": error.detail,
                               "headers": error.headers or {}})
            except Exception as error:
                details = model_error_details(error, stage="chat_stream")
                emit("error", {"status": 503,
                    "detail": safe_model_error_message(details)
                    + "请刷新核实会话结果后，再决定是否恢复或开启新对话。"})
            else:
                emit("done", result)

        async def worker():
            try:
                # Disconnect only stops delivery, not an in-flight write or checkpoint.
                await anyio.to_thread.run_sync(execute, abandon_on_cancel=False)
            finally:
                await sender.aclose()

        def encode(event):
            kind, data = event
            return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        async with anyio.create_task_group() as group:
            async with receiver:
                group.start_soon(worker)
                first = await receiver.receive()
                if first[0] == "error":
                    response = JSONResponse({"detail": first[1]["detail"]},
                                            status_code=first[1]["status"],
                                            headers=first[1].get("headers"))
                else:
                    async def events():
                        yield encode(first)
                        async for event in receiver:
                            yield encode(event)

                    response = StreamingResponse(events(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
                try:
                    await response(scope, receive, send)
                finally:
                    disconnected.set()
                    await receiver.aclose()
