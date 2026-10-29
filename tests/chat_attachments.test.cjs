"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.value = ""; this._text = ""; this.dataset = {}; this.disabled = false;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(""); }
  set innerHTML(_) { throw new Error("Unsafe HTML"); }
  append(...nodes) { nodes.forEach((node) => { node.parent = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this._text = ""; this.children = []; this.append(...nodes); }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  setAttribute() {}
  scrollIntoView() {}
  focus() {}
  close() {}
  remove() { this.parent.children = this.parent.children.filter((item) => item !== this); }
  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(child.tagName === selector.toUpperCase() ? [child] : []), ...child.querySelectorAll(selector)]);
  }
}
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return {resolve, promise}; };
const tick = () => new Promise((r) => setImmediate(r));
const attachment = (name = "test.xml") => ({attachment_id: "a".repeat(32), filename: name,
  attachment_kind: "invoice_xml", attachment_status: "parsed"});
const file = (name = "test.xml", bytes = Buffer.from("\ufeff<EInvoice>测试</EInvoice>")) => ({name, size: bytes.length, bytes});

async function harness(inspect) {
  const nodes = new Map(), calls = [], storage = new Map();
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const conversations = ["one", "two"].map((id) => ({id, title: id, updated_at: "2026-10-27T00:00:00Z"}));
  const context = vm.createContext({
    console, Response, TextDecoder, crypto: require("node:crypto").webcrypto,
    setTimeout, window: {addEventListener() {}, setInterval() {}},
    document: {getElementById: get, createElement: (tag) => new Element(tag)},
    sessionStorage: {getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key)},
    FileReader: class {
      readAsDataURL(value) { this.result = "data:application/xml;base64," + value.bytes.toString("base64"); this.onload(); }
    },
    renderChatText: (row, text) => { row.textContent = text; },
    fetch: async (url, options = {}) => {
      calls.push({url, options});
      let result;
      if (url === "/api/auth/me") result = {user_id: "alice", username: "alice"};
      else if (url.startsWith("/api/conversations?") || url === "/api/conversations") result = {items: conversations, next_offset: null};
      else if (url.endsWith("/runs")) result = {run: null};
      else if (url.endsWith("/chat")) result = {messages: [], interrupt: [], revision: "v1", conversation_id: url.split("/")[3], run: null};
      else if (url === "/api/attachments/config") result = {max_files: 2, max_file_bytes: 1024};
      else if (url === "/api/attachments/inspect") return inspect(url, options);
      else if (url.endsWith("/chat/stream")) {
        const body = JSON.parse(options.body);
        const payload = {messages: [{role: "user", text: body.message || "", attachments: body.attachment_ids.map(() => attachment())}],
          interrupt: [], revision: "v2", conversation_id: url.split("/")[3], run: {id: body.request_id, status: "completed"}};
        return new Response("event: done\ndata: " + JSON.stringify(payload) + "\n\n",
          {headers: {"Content-Type": "text/event-stream"}});
      } else throw new Error("Unexpected URL " + url);
      return Response.json(result);
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../business_travel/web_static/chat-stream.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../business_travel/web_static/app.js"), "utf8"), context);
  for (let i = 0; i < 12; i++) await tick();
  return {get, calls, context, run: (code) => vm.runInContext(code, context), upload: async (files) => {
    get("chat-files").files = files;
    await get("chat-files").listeners.change({target: get("chat-files")});
  }};
}

test("upload uses unchanged bytes, shows a safe name and sends only IDs with user text", async () => {
  let sent;
  const name = '<img onerror="bad()">.xml';
  const h = await harness(async (_, options) => { sent = JSON.parse(options.body); return Response.json(attachment(name)); });
  const original = file(name);
  await h.upload([original]);
  assert.deepEqual(Buffer.from(sent.content_base64, "base64"), original.bytes);
  assert.equal(h.get("chat-attachment-list").querySelectorAll("img").length, 0);
  assert.match(h.get("chat-attachment-list").textContent, /尚未用于聊天核对/);
  await h.run('sendChat({message: "请核对"})');
  const request = JSON.parse(h.calls.find((call) => call.url.endsWith("/chat/stream")).options.body);
  assert.deepEqual(request.attachment_ids, ["a".repeat(32)]);
  assert.equal(request.message, "请核对");
  assert.ok(!request.content_base64);
  assert.equal(h.get("chat-attachment-list").children.length, 0);
  assert.match(h.get("chat-messages").textContent, /附件：/);
});

test("upload blocks sending and conversation switches until it completes", async () => {
  const pending = deferred(), h = await harness(() => pending.promise);
  const uploading = h.upload([file()]);
  await tick();
  assert.equal(h.get("chat-submit").disabled, true);
  assert.equal(h.get("chat-new").disabled, true);
  await h.run('loadChat("two")');
  await h.run('sendChat({message: "must not send"})');
  assert.equal(h.run("conversationId"), "one");
  assert.ok(!h.calls.some((call) => call.url.endsWith("/chat/stream")));
  pending.resolve(Response.json(attachment())); await uploading;
  assert.equal(h.get("chat-submit").disabled, false);
});

test("successful files survive a later file failure, and draft attachments stay in their conversation", async () => {
  let count = 0;
  const h = await harness(async () => ++count === 1 ? Response.json(attachment()) : Response.json({detail: "保存失败"}, {status: 503}));
  await h.upload([file(), file("bad.xml")]);
  assert.equal(h.get("chat-attachment-list").children.length, 1);
  assert.match(h.get("chat-attachment-status").textContent, /成功的附件仍保留/);
  await h.run('loadChat("two")');
  assert.equal(h.get("chat-attachment-list").children.length, 0);
  await h.run('loadChat("one")');
  assert.equal(h.get("chat-attachment-list").children.length, 1);
  assert.equal(h.get("chat-attachment-list").querySelectorAll("button")[0].disabled, false);
});

test("logout discards a late upload and does not repopulate another account", async () => {
  const pending = deferred(), h = await harness(() => pending.promise);
  const uploading = h.upload([file()]); await tick();
  h.run("showLogin()");
  pending.resolve(Response.json(attachment())); await uploading;
  assert.equal(h.run("chatAttachments.size"), 0);
  assert.equal(h.get("chat-attachment-list").children.length, 0);
  assert.equal(h.get("chat-files").disabled, true);
});

test("count and byte limits reject locally before upload; removal and resume cannot send stale IDs", async () => {
  const h = await harness(async () => Response.json(attachment()));
  await h.upload([file(), file(), file()]);
  assert.match(h.get("chat-attachment-status").textContent, /数量超过/);
  await h.upload([file("large.xml", Buffer.alloc(1025))]);
  assert.ok(!h.calls.some((call) => call.url.endsWith("/inspect")));
  await h.upload([file()]);
  h.get("chat-attachment-list").querySelectorAll("button")[0].listeners.click();
  assert.equal(h.get("chat-attachment-list").children.length, 0);
  assert.equal(h.get("chat-attachment-status").textContent, "");
  await h.upload([file()]);
  await h.run('sendChat({resume: true, revision: "v1"})');
  const payload = JSON.parse(h.calls.find((call) => call.url.endsWith("/chat/stream")).options.body);
  assert.deepEqual(payload.attachment_ids, []);
});

test("removing an attachment updates the remaining count without removing other files", async () => {
  let count = 0;
  const h = await harness(async () => Response.json({...attachment(),
    attachment_id: (++count).toString(16).padStart(32, "0")}));
  await h.upload([file("first.xml"), file("second.xml")]);
  h.get("chat-attachment-list").querySelectorAll("button")[0].listeners.click();
  assert.equal(h.get("chat-attachment-list").children.length, 1);
  assert.equal(h.get("chat-attachment-status").textContent, "已关联 1 个附件，发送文字后提交。");
});
