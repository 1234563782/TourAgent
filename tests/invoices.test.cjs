"use strict";
const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

class Element {
  constructor(tag = "div") { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.value = ""; this._text = ""; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((node) => node.textContent).join(""); }
  set innerHTML(_) { throw new Error("Unsafe HTML rendering"); }
  append(...nodes) { nodes.forEach((node) => { node.parent = this; this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = []; this._text = ""; this.append(...nodes); }
  replaceWith(node) { const i = this.parent.children.indexOf(this); node.parent = this.parent; this.parent.children[i] = node; }
  setAttribute() {}
  addEventListener(type, handler) { this.listeners[type] = handler; }
  reset() { this.value = ""; this.children.forEach((node) => node.reset()); }
  querySelectorAll(selector) {
    const matches = [], tags = selector.toUpperCase().split(",");
    for (const child of this.children) { if (tags.includes(child.tagName)) matches.push(child); matches.push(...child.querySelectorAll(selector)); }
    return matches;
  }
}
function harness() {
  const nodes = new Map();
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  class FormData {
    constructor(form) { this.form = form; }
    *[Symbol.iterator]() {
      for (const node of this.form.querySelectorAll("input,textarea,select")) {
        if (node.disabled || (node.type === "checkbox" && !node.checked)) continue;
        yield [node.name, node.value];
      }
    }
  }
  const window = {}, context = vm.createContext({window, FormData, Uint8Array, btoa,
    document: {getElementById: get, createElement: (tag) => new Element(tag)}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../business_travel/web_static/invoices.js"), "utf8"), context);
  return {ui: window.InvoiceReviewUI, get};
}
const config = {max_file_bytes: 1048576, max_batch_files: 10, buyer_configured: true};
function result(filename = "test.xml") {
  return {filename, verdict: "local_checks_passed", reasons: [], policy_lines: [], file_hash: "a".repeat(64),
    notice: "未官方查验，不是报销批准", verification_status: "not_checked",
    invoice: {number: "26000000000000000001", issue_date: "2026-09-20", amount_total: "1000.00",
      buyer_name: "测试大学", seller_name: "测试宾馆", signature_status: "present_not_verified", evidence: {}}};
}
const submit = (form) => form.listeners.submit({preventDefault() {}});
function file(bytes, name = "test.xml") { return {name, size: bytes.length, arrayBuffer: async () => Uint8Array.from(bytes).buffer}; }
const deferred = () => { let resolve; const promise = new Promise((r) => {resolve = r;}); return {promise, resolve}; };

test("configuration and old requests cannot repopulate a signed-out workspace", async () => {
  const {ui, get} = harness(), pending = deferred();
  const active = ui.activate(() => pending.promise, () => {});
  ui.reset(); pending.resolve(config); await active;
  assert.equal(get("invoice-submit").disabled, true);
  assert.equal(get("invoice-results").children.length, 0);
});

test("upload preserves original UTF-8 bytes and renders untrusted filenames as text", async () => {
  const {ui, get} = harness(), bytes = Buffer.from("\ufeff<EInvoice>测试</EInvoice>"), calls = [];
  const attack = '<img src=x onerror="bad()">.xml';
  await ui.activate(async (url, options) => { calls.push({url, options}); return options ? {results: [result(attack)]} : config; }, () => {});
  get("invoice-files").files = [file(bytes, attack)]; await submit(get("invoice-form"));
  const payload = JSON.parse(calls[1].options.body);
  assert.deepEqual(Buffer.from(payload.files[0].content_base64, "base64"), bytes);
  assert.deepEqual(payload.files[0].expenses, []);
  assert.ok(get("invoice-results").textContent.includes(attack));
  assert.ok(get("invoice-results").textContent.includes("不是报销批准"));
  assert.equal(get("invoice-results").querySelectorAll("img,script").length, 0);
});

test("a pending review never leaks into a later login", async () => {
  const {ui, get} = harness(), pending = deferred();
  await ui.activate(async (_, options) => options ? pending.promise : config, () => {});
  get("invoice-files").files = [file(Buffer.from("xml"))];
  const inflight = submit(get("invoice-form"));
  await Promise.resolve(); await Promise.resolve();
  ui.reset(); pending.resolve({results: [result()]}); await inflight;
  assert.equal(get("invoice-results").children.length, 0);
  assert.equal(get("invoice-submit").disabled, true);
});

test("upload failure keeps previous results and exposes an actionable error", async () => {
  const {ui, get} = harness(); let fail = false;
  await ui.activate(async (_, options) => {
    if (!options) return config;
    if (fail) throw new Error("网络不可用，请重试");
    return {results: [result()]};
  }, () => {});
  get("invoice-files").files = [file(Buffer.from("xml"))]; await submit(get("invoice-form"));
  fail = true; await submit(get("invoice-form"));
  assert.equal(get("invoice-results").children.length, 1);
  assert.ok(get("invoice-error").textContent.includes("请重试"));
  assert.equal(get("invoice-submit").disabled, false);
});

test("hotel supplements preserve nightly entries, do not average, and send no rank or verified flag", async () => {
  const {ui, get} = harness(), calls = [];
  await ui.activate(async (_, options) => { if (!options) return config; calls.push(JSON.parse(options.body)); return {results: [result()]}; }, () => {});
  get("invoice-files").files = [file(Buffer.from("xml"))]; await submit(get("invoice-form"));
  const form = get("invoice-results").querySelectorAll("form")[0];
  const fields = Object.fromEntries(form.querySelectorAll("input,select,textarea").map((node) => [node.name, node]));
  // The fake DOM does not implement native select defaults.
  fields.kind.value = "hotel"; fields.kind.listeners.change();
  fields.expense_date.value = "2026-09-16"; fields.check_out.value = "2026-09-18";
  fields.destination.value = "北京"; fields.destination_province.value = "北京";
  fields.nightly.value = "2026-09-16,400.00\n2026-09-17,600.00";
  await submit(form);
  const facts = calls[1].files[0].expenses[0].facts;
  assert.deepEqual(facts.nightly_amounts, [{date: "2026-09-16", amount: "400.00"}, {date: "2026-09-17", amount: "600.00"}]);
  assert.equal(facts.amount_scope, "stay_total"); assert.equal(facts.amount, "1000.00");
  assert.equal(facts.date_verified, undefined); assert.equal(facts.lodging_standard, undefined);
});

test("an expired session triggers clearing, not an invoice retry", async () => {
  const {ui, get} = harness(); let expired = 0;
  await ui.activate(async (_, options) => { if (!options) return config; throw Object.assign(new Error("登录失效"), {status: 401}); },
    () => { expired++; ui.reset(); });
  get("invoice-files").files = [file(Buffer.from("xml"))]; await submit(get("invoice-form"));
  assert.equal(expired, 1); assert.equal(get("invoice-results").children.length, 0);
});

test("policy results show the engine status, reference amount and concrete missing fields", async () => {
  const {ui, get} = harness(), reviewed = result();
  reviewed.policy_lines = [
    {line_id: "1", status: "within_checked_rules", policy_result: {
      estimate: {reference_amount: "1000.00", currency: "CNY"}}},
    {line_id: "2", status: "insufficient_information", missing_fields: ["nightly_amounts", "check_out"]}
  ];
  await ui.activate(async (_, options) => options ? {results: [reviewed]} : config, () => {});
  get("invoice-files").files = [file(Buffer.from("xml"))]; await submit(get("invoice-form"));
  const content = get("invoice-results").textContent;
  assert.ok(content.includes("费用行 1 · 差标：已核对一般差标"));
  assert.ok(content.includes("参考金额：1,000.00 CNY；不是票据验真或报销批准"));
  assert.ok(content.includes("还需补充 / 核实：逐晚含税金额 / 水单、退房日期"));
});

test("invoice amounts retain exact decimal digits beyond JavaScript integer precision", async () => {
  const {ui, get} = harness(), reviewed = result();
  reviewed.invoice.amount_total = "9007199254740993.01";
  await ui.activate(async (_, options) => options ? {results: [reviewed]} : config, () => {});
  get("invoice-files").files = [file(Buffer.from("xml"))]; await submit(get("invoice-form"));
  assert.ok(get("invoice-results").textContent.includes("9,007,199,254,740,993.01"));
});

test("unrelated XML is explained as unsupported and not treated as an invoice", async () => {
  const {ui, get} = harness(), reviewed = result();
  reviewed.verdict = "unsupported";
  reviewed.invoice = null;
  reviewed.attachment_kind = "xml_other";
  reviewed.attachment_status = "unsupported";
  reviewed.reasons = [{code: "unsupported_layout", detail: "不是当前支持的数电票 XML"}];
  await ui.activate(async (_, options) => options ? {results: [reviewed]} : config, () => {});
  get("invoice-files").files = [file(Buffer.from("<TravelOrder/>"))]; await submit(get("invoice-form"));
  const content = get("invoice-results").textContent;
  assert.ok(content.includes("不是当前支持的数电票结构"));
  assert.ok(content.includes("未登记发票、未查重"));
});
