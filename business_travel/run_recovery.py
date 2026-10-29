"""失败执行的只读核实和事务性分叉，不重放模型、工具或待确认操作。"""

from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .chat_runs import REVIEW_STATUSES, public_run
from .memory_pg import ScopedPostgresSaver, memory_connection


class RecoveryConflict(Exception):
    pass


class RecoveryUnavailable(Exception):
    pass


class RecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    review_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: StrictBool

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError("Explicit review required")
        return self


def recovery_result(row):
    return {"conversation_id": row["target_conversation_id"],
            "source_run_id": row["run_id"], "revision": row["target_checkpoint_id"]}


def _review(connection, identity, conversation_id, graph):
    scope = (identity.user_id, conversation_id)
    run = connection.execute(
        'SELECT * FROM chat_runs WHERE user_id=%s AND conversation_id=%s ORDER BY created_at DESC,id DESC LIMIT 1', scope).fetchone()
    if not run or run["status"] not in REVIEW_STATUSES:
        raise RecoveryConflict("当前没有待核实的失败执行，请刷新会话")
    recovered = connection.execute(
        'SELECT * FROM chat_recoveries WHERE user_id=%s AND conversation_id=%s AND run_id=%s', (*scope, run["id"])).fetchone()
    if recovered:
        return {"run": public_run(run), "recovery": recovery_result(recovered)}, None

    completed = connection.execute(
        "SELECT checkpoint_id FROM chat_runs WHERE user_id=%s AND conversation_id=%s AND status='completed' ORDER BY created_at DESC,id DESC LIMIT 1",
        scope).fetchone()
    # A fork can itself fail before its first successful turn; retain its original seed.
    inherited = connection.execute(
        'SELECT target_checkpoint_id FROM chat_recoveries WHERE user_id=%s AND target_conversation_id=%s', scope).fetchone()
    checkpoint_id = (completed["checkpoint_id"] if completed else
                     inherited["target_checkpoint_id"] if inherited else None)
    values = {}
    if checkpoint_id:
        config = {"configurable": {"thread_id": identity.thread_id(conversation_id),
                                    "checkpoint_id": checkpoint_id}}
        # get_state can echo a requested checkpoint ID even when storage is empty.
        if graph.checkpointer.get_tuple(config) is None:
            raise RecoveryUnavailable("完整检查点已缺失，不能安全继承，请保留原会话并联系管理员")
        state = graph.get_state(config)
        if ((state.config or {}).get("configurable", {}).get("checkpoint_id") != checkpoint_id
                or state.next or state.interrupts or any(task.error for task in state.tasks)):
            raise RecoveryUnavailable("完整检查点不可用，不能安全继承，请保留原会话并联系管理员")
        values = state.values
    trips = connection.execute(
        'SELECT draft_id,data,saved_at FROM trips WHERE user_id=%s AND saved_at >= %s ORDER BY sequence', (*scope[:1], run["created_at"])).fetchall()
    review = {
        "run": public_run(run), "source_checkpoint_id": checkpoint_id,
        "saved_trips": [{"id": row["draft_id"], "data": row["data"],
                         "saved_at": row["saved_at"].isoformat()} for row in trips],
        "review_since": run["created_at"].isoformat(),
        "history_message_count": len(values.get("messages", [])),
        "notice": "失败执行结果尚未确认。以下是本次请求开始后已保存的行程草案，"
                  "可能包含其他会话的写入，不代表订单或预订成功。分叉仅继承最近完整完成的对话，"
                  "不继承失败轮和待确认操作；已保存的个人记录不会撤销，也不会重试原请求。",
        "recovery": None,
    }
    review["review_revision"] = sha256(json.dumps(
        {"review": review, "run_updated_at": run["updated_at"].isoformat()},
        sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return review, values


def recover_run(settings, identity, conversation_id, graph_factory, body=None):
    """Caller holds the source conversation lock. All fork writes commit together."""
    with memory_connection(settings) as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        graph = graph_factory(ScopedPostgresSaver(connection, identity))
        review, values = _review(connection, identity, conversation_id, graph)
        if body is None:
            return review
        if body.run_id != review["run"]["id"]:
            raise RecoveryConflict("待核实的运行已变化，请重新核实")
        if review["recovery"]:
            receipt = connection.execute(
                'SELECT review_revision FROM chat_recoveries WHERE user_id=%s AND conversation_id=%s AND run_id=%s',
                (identity.user_id, conversation_id, body.run_id)).fetchone()
            if receipt["review_revision"] != body.review_revision:
                raise RecoveryConflict("此运行已有恢复会话，请刷新查看")
            return review["recovery"]
        if body.review_revision != review["review_revision"]:
            raise RecoveryConflict("个人记录或执行状态已变化，请重新核实后确认")
        ident = uuid4().hex
        scope = (identity.user_id,)
        connection.execute(
            'INSERT INTO conversations(user_id,id,title) VALUES(%s,%s,%s)',
            (*scope, ident, "恢复对话"))
        # Only committed durable context is inherited. Queue, draft, pending consent
        # and graph runtime internals never cross into the new execution.
        seed = {key: deepcopy(values[key]) for key in ("messages", "slots", "tasks") if key in values}
        seed.setdefault("messages", [])
        config = {"configurable": {"thread_id": identity.thread_id(ident)}}
        # update_state schedules no work after the graph's existing terminal node.
        saved = graph.update_state(config, seed, as_node="finish")
        state = graph.get_state(saved)
        if state.next or state.interrupts:
            raise RecoveryUnavailable("恢复检查点仍有待执行操作，已取消分叉")
        checkpoint_id = saved["configurable"]["checkpoint_id"]
        row = connection.execute(
            'INSERT INTO chat_recoveries(user_id,conversation_id,run_id,review_revision,source_checkpoint_id,target_conversation_id,target_checkpoint_id) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',
            (*scope, conversation_id, body.run_id, body.review_revision,
             review["source_checkpoint_id"], ident, checkpoint_id)).fetchone()
        return recovery_result(row)
