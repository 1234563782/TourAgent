// Isolated UI acceptance using the real frontend and Python renderer.
// All API responses are fixtures; no real account, supplier or database is used.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const output = path.resolve(process.env.PRESENTATION_TEST_OUTPUT || path.join(root, ".verification", "presentation"));
const origin = "http://127.0.0.1:48173";
const text = execFileSync(path.join(root, ".venv", "Scripts", "python.exe"), ["-c",
  "from tests.test_plan_presentation import draft; from business_travel.plan_checks import render_draft; print(render_draft(draft()))"],
{ cwd: root, encoding: "utf8", env: { ...process.env, PYTHONIOENCODING: "utf-8" } }).trim();
const conversation = { id: "presentation-fixture", title: "展示验收（模拟数据）", updated_at: "2026-09-18T10:00:00Z" };
let state = { conversation_id: conversation.id, messages: [], revision: null, interrupt: [], run: null };
const errors = [];

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true, ...(process.env.AUTH_TEST_BROWSER ? { executablePath: process.env.AUTH_TEST_BROWSER } : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin);
      const pathname = url.pathname;
      const json = (data) => route.fulfill({ json: data });
      if (pathname === "/") return route.fulfill({
        path: path.join(root, "business_travel", "web_static", "index.html"), contentType: "text/html",
      });
      if (pathname.startsWith("/static/")) {
        const name = path.basename(pathname);
        assert.ok(["app.css", "app.js", "message-view.js", "chat-stream.js", "brand.svg"].includes(name));
        return route.fulfill({ path: path.join(root, "business_travel", "web_static", name),
          contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "image/svg+xml" });
      }
      if (pathname === "/api/auth/me") return json({ username: "展示验收", company_id: "fixture", user_id: "fixture" });
      if (pathname === "/api/memory") return json({ preferences: [], trip_history: [] });
      if (pathname === "/api/preferences") return json({ items: [], revision: "fixture" });
      if (pathname === "/api/conversations") return json({ items: [conversation], next_offset: null });
      if (pathname.endsWith("/runs")) return json({ run: state.run });
      if (pathname.endsWith("/chat")) return json(state);
      if (pathname.endsWith("/chat/stream")) {
        const request = route.request().postDataJSON();
        const run = { id: request.request_id, status: "completed" };
        state = { ...state, run, revision: "fixture-1", messages: [
          { role: "user", text: request.message }, { role: "assistant", text },
        ] };
        const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
        let body = frame("progress", { run_id: run.id, text: "整理参考方案" });
        for (let start = 0; start < text.length; start += 37) {
          body += frame("delta", { run_id: run.id, text: text.slice(start, start + 37) });
        }
        body += frame("done", state);
        return route.fulfill({ contentType: "text/event-stream", body });
      }
      throw new Error("Unexpected fixture request: " + pathname);
    });
    await page.goto(origin);
    await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
    await page.evaluate(() => {
      window.renderSamples = [];
      const render = window.renderChatText;
      window.renderChatText = (...args) => {
        render(...args);
        if (args[2]?.streaming) {
          window.renderSamples.push({ characters: args[1].length,
            tables: args[0].querySelectorAll("table").length,
            details: args[0].querySelectorAll("details").length });
        }
      };
    });
    await page.locator("#chat-input").fill("请按已有条件给我一份差旅参考方案");
    await page.locator("#chat-submit").click();
    await page.waitForFunction(() => document.querySelector(".chat-message.assistant table")
      && !document.getElementById("chat-submit").disabled);
    const answer = page.locator(".chat-message.assistant").last();
    assert.equal(await answer.locator("tbody tr").count(), 2);
    assert.equal(await answer.locator("details[open]").count(), 0);
    assert.equal(await answer.locator("details").count(), 2);
    assert.match(await answer.innerText(), /270.5 CNY/);
    assert.doesNotMatch(await answer.innerText(), /供应商默认|返回库存|internal-/);
    const samples = await page.evaluate(() => window.renderSamples);
    assert.ok(samples.length > 10);
    assert.ok(samples.some((sample) => sample.tables && sample.characters < text.length));
    assert.ok(samples.some((sample) => sample.details && sample.characters < text.length));
    const snapshot = await answer.innerHTML();
    await answer.locator("summary").first().click();
    assert.match(await answer.innerText(), /取消收取全款/);
    await answer.locator("summary").first().click();
    await page.reload();
    await page.waitForFunction(() => document.querySelector(".chat-message.assistant table"));
    assert.equal(await page.locator(".chat-message.assistant").last().innerHTML(), snapshot);
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 });
      await page.locator("#chat-messages").evaluate((node) => { node.scrollTop = 0; });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const table = page.locator(".chat-table");
      const box = await table.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width);
      const input = await page.locator("#chat-input").boundingBox();
      const button = await page.locator("#chat-submit").boundingBox();
      assert.ok(input.width > 100 && button.x >= input.x + input.width);
      await page.screenshot({ path: path.join(output, `presentation-${width}.png`), fullPage: true });
      await page.locator("#chat-messages").evaluate((node) => { node.scrollTop = node.scrollHeight; });
      await page.screenshot({ path: path.join(output, `presentation-summary-${width}.png`), fullPage: true });
      await page.locator(".chat-message.assistant summary").first().click();
      await page.screenshot({ path: path.join(output, `presentation-details-${width}.png`), fullPage: true });
      await page.locator(".chat-message.assistant summary").first().click();
    }
    const security = await page.evaluate(() => {
      const node = document.createElement("div");
      const attack = '<img src=x onerror="window.PWNED=true"><script>window.PWNED=true</script>';
      renderChatText(node, `### ${attack}\n:::details ${attack}\n${attack}\n:::`);
      document.body.append(node);
      const safe = node.querySelectorAll("img,script,a").length === 0 && !window.PWNED;
      const detail = node.querySelector("details");
      detail.open = true;
      detail.querySelector("summary").focus();
      renderChatText(node, `### ${attack}\n:::details ${attack}\n${attack}\n:::\nMore text`, { streaming: true });
      const stable = node.querySelector("details").open && document.activeElement.tagName === "SUMMARY";
      node.remove();
      return { safe, stable };
    });
    assert.deepEqual(security, { safe: true, stable: true });
    assert.deepEqual(errors, []);
    const report = { passed: true, mode: "isolated-fixtures", streamFrames: samples.length,
      checks: ["real-app-SSE-handler", "progressive-tables-and-details", "collapsed-default",
        "expand-cancellation", "reload-identical", "desktop-1440", "mobile-390", "mobile-320",
        "inert-html", "stream-preserves-expanded-state-and-focus"],
      boundary: "No real model, supplier, account, database or running service was used." };
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
