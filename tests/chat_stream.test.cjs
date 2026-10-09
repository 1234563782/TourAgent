const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const context = vm.createContext({ TextDecoder });
vm.runInContext(fs.readFileSync(path.join(__dirname,
  "../business_travel/web_static/chat-stream.js"), "utf8"), context);
const readChatEvents = context.readChatEvents;
const frame = (kind, data) => `event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;

function response(text, chunkSize = 1) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream({
    start(controller) {
      for (let index = 0; index < bytes.length; index += chunkSize) {
        controller.enqueue(bytes.slice(index, index + chunkSize));
      }
      controller.close();
    },
  }), { headers: { "Content-Type": "text/event-stream" } });
}

test("UTF-8 and event frames survive arbitrary byte boundaries", async () => {
  const events = [];
  const done = { messages: [{ role: "assistant", text: "杭州酒店" }], conversation_id: "a" };
  const result = await readChatEvents(response(
    frame("progress", { text: "正在查询酒店" }) +
    frame("delta", { text: "杭州酒店" }) + frame("done", done)),
  (kind, data) => events.push([kind, data.text]));
  assert.deepEqual(events, [["progress", "正在查询酒店"], ["delta", "杭州酒店"]]);
  assert.equal(result.messages[0].text, "杭州酒店");
});

test("async event callbacks are awaited between streamed frames", async () => {
  let callbackCompleted = false;
  const result = await readChatEvents(response(
    frame("delta", { text: "第一段" }) +
    frame("done", { messages: [{ role: "assistant", text: "第一段" }] })),
  async (kind) => {
    if (kind !== "delta") return;
    await new Promise((resolve) => setTimeout(resolve, 10));
    callbackCompleted = true;
  });
  assert.equal(callbackCompleted, true);
  assert.equal(result.messages[0].text, "第一段");
});

test("EOF without done is a failure, even when a delta was received", async () => {
  await assert.rejects(readChatEvents(response(frame("delta", { text: "暂定结果" })),
    () => {}), /Incomplete response/);
});

test("explicit failure cannot become success", async () => {
  await assert.rejects(readChatEvents(response(frame("error",
    { status: 503, detail: "请刷新核实" })), () => {}),
  (error) => error.status === 503 && error.message === "请刷新核实");
});

test("invalid committed payload is rejected", async () => {
  await assert.rejects(readChatEvents(response(frame("done",
    { messages: [{ role: "tool", text: "internal" }] })), () => {}),
  /Invalid committed response/);
});

test("run status gates running and uncertain operations, not completed answers", () => {
  const run = { id: "a".repeat(32), status: "running", progress: { text: "正在查询酒店" } };
  assert.equal(context.chatRunState(run).blocked, true);
  assert.match(context.chatRunState(run).text, /正在查询酒店/);
  for (const status of ["failed", "interrupted"]) {
    assert.equal(context.chatRunState({ ...run, status }).blocked, true);
    assert.match(context.chatRunState({ ...run, status }).text, /不会自动重试/);
  }
  for (const status of ["completed", "awaiting_confirmation"]) {
    assert.equal(context.chatRunState({ ...run, status }).blocked, false);
  }
  assert.equal(context.chatRunState(null).blocked, false);
  assert.throws(() => context.chatRunState({ ...run, status: "unknown" }), /Invalid run status/);
  assert.throws(() => context.chatRunState({ ...run, id: "../foreign" }), /Invalid run identity/);
});
