// Real frontend, isolated API fixtures. No real account, model or database writes.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const output = path.resolve(process.env.AUTH_TEST_OUTPUT || path.join(root, ".verification", "preference-autosave-ui"));
const origin = "http://127.0.0.1:48174";

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    const requests = [];
    let authenticated = false;
    let failLogout = false;
    let sequence = 0;
    const conversations = [{ id: "fixture-first", title: "Test conversation", updated_at: new Date().toISOString() }];
    const states = new Map();
    const empty = (id) => ({ conversation_id: id, messages: [], revision: null, interrupt: [], run: null });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin);
      const pathname = url.pathname;
      requests.push(pathname);
      const json = (data, status = 200) => route.fulfill({ json: data, status });
      if (pathname === "/") return route.fulfill({
        path: path.join(root, "business_travel", "web_static", "index.html"), contentType: "text/html",
      });
      if (pathname.startsWith("/static/")) {
        const name = path.basename(pathname);
        assert.ok(["app.css", "app.js", "message-view.js", "chat-stream.js", "brand.svg"].includes(name));
        return route.fulfill({ path: path.join(root, "business_travel", "web_static", name),
          contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "image/svg+xml" });
      }
      if (pathname === "/api/auth/login") {
        authenticated = true;
        return json({ ok: true });
      }
      if (!authenticated) return json({ detail: "Session expired" }, 401);
      if (pathname === "/api/auth/me") return json({ username: "Fixture", company_id: "fixture", user_id: "fixture" });
      if (pathname === "/api/auth/logout") {
        if (failLogout) return json({ detail: "Synthetic outage" }, 503);
        authenticated = false;
        return json({ ok: true });
      }
      if (pathname === "/api/conversations") {
        if (route.request().method() === "POST") {
          const item = { id: `fixture-${++sequence}`, title: "New conversation", updated_at: new Date().toISOString() };
          conversations.unshift(item);
          return json(item);
        }
        return json({ items: conversations, next_offset: null });
      }
      const id = pathname.split("/")[3];
      const state = states.get(id) || empty(id);
      if (pathname.endsWith("/runs")) return json({ run: state.run });
      if (pathname.endsWith("/chat")) return json(state);
      if (pathname.endsWith("/chat/stream")) {
        const request = route.request().postDataJSON();
        const text = "已记住你的偏好。";
        const run = { id: request.request_id, status: "completed" };
        const result = { ...state, run, revision: `revision-${sequence}`, messages: [
          ...state.messages, { role: "user", text: request.message }, { role: "assistant", text },
        ] };
        states.set(id, result);
        const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
        const body = frame("progress", { run_id: run.id, text: "正在处理" })
          + [...text].map((character) => frame("delta", { run_id: run.id, text: character })).join("")
          + frame("done", result);
        return route.fulfill({ contentType: "text/event-stream", body });
      }
      throw new Error("Unexpected fixture request: " + pathname);
    });
    const login = async () => {
      await page.locator("#username").fill("fixture");
      await page.locator("#password").fill("synthetic-password-only");
      await page.locator("#login-submit").click();
      await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
    };
    await page.goto(origin);
    await page.locator("#login-view").waitFor({ state: "visible" });
    await login();
    assert.equal(await page.locator("#records, #preferences-tab, #preference-dialog, #delete-dialog").count(), 0);
    await page.evaluate(() => {
      window.streamRenders = 0;
      const original = window.renderChatText;
      window.renderChatText = (...args) => {
        if (args[2]?.streaming) window.streamRenders++;
        return original(...args);
      };
    });
    await page.locator("#chat-input").fill("我平时喜欢坐火车");
    await page.locator("#chat-submit").click();
    await page.waitForFunction(() => !document.getElementById("chat-submit").disabled
      && document.querySelector(".chat-message.assistant")?.textContent.includes("已记住"));
    assert.ok(await page.evaluate(() => window.streamRenders > 1));
    assert.equal(await page.locator("#chat-confirm").isVisible(), false);
    await page.reload();
    await page.locator(".chat-message.assistant").waitFor();
    await page.locator("#chat-reload").click();
    await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
    assert.equal(await page.locator(".chat-message").count(), 2);
    await page.locator("#chat-new").click();
    await page.waitForFunction(() => document.querySelectorAll("#conversation-list button").length === 2
      && !document.getElementById("chat-submit").disabled);
    assert.equal(await page.locator(".chat-message").count(), 0);
    await page.locator('[data-conversation-id="fixture-first"]').click();
    await page.locator(".chat-message.assistant").waitFor();
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const input = await page.locator("#chat-input").boundingBox();
      const send = await page.locator("#chat-submit").boundingBox();
      assert.ok(input.width > 100 && input.height > 40 && send.width > 20);
      assert.ok(input.y + input.height <= send.y || input.x + input.width <= send.x);
      assert.equal(await page.locator(".brand img").evaluate((image) => image.complete && image.naturalWidth > 0), true);
      await page.screenshot({ path: path.join(output, `chat-${width}.png`), fullPage: true });
    }
    failLogout = true;
    await page.locator("#logout").click();
    await page.locator("#chat-error").filter({ hasText: "退出未完成" }).waitFor();
    assert.equal(await page.locator("#workspace").isVisible(), true);
    failLogout = false;
    await page.locator("#logout").click();
    await page.locator("#login-view").waitFor({ state: "visible" });
    await login();
    authenticated = false;
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.locator("#login-view").waitFor({ state: "visible" });
    assert.equal(await page.locator(".chat-message").count(), 0);
    assert.equal(requests.some((url) => ["/api/memory", "/api/preferences"].includes(url)), false);
    assert.deepEqual(errors, []);
    const result = { status: "passed", mode: "isolated-api-fixtures", widths: [1440, 390, 320],
      checks: ["removed-panels", "no-memory-requests", "login", "sse", "no-preference-confirmation",
        "reload", "new-conversation", "switch-conversation", "logout-failure", "logout", "session-expiry"] };
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
