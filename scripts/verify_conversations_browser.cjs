// Only the isolated verification server uses a deterministic graph.
const { chromium, expect } = require("playwright/test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const { randomUUID } = require("node:crypto");

(async () => {
  const origin = process.env.AUTH_TEST_ORIGIN;
  const output = process.env.AUTH_TEST_OUTPUT;
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  const passed = [];
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.name));
    const post = (route, data) => context.request.post(origin + route, {
      headers: { Origin: origin }, data,
    });
    const ready = () => expect(page.locator("#chat-new")).toBeEnabled();
    const selected = () => page.locator('#conversation-list [aria-current="true"]').getAttribute("data-conversation-id");
    const login = async (username) => {
      await page.getByLabel("账号", { exact: true }).fill(username);
      await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
      await page.locator("#login-submit").click();
      await expect(page.locator("#workspace")).toBeVisible();
      await expect(page.locator("#chat-input")).toBeEnabled();
    };
    const send = async (text) => {
      await page.locator("#chat-input").fill(text);
      await page.locator("#chat-submit").click();
      await expect(page.locator("#chat-messages .user").last()).toHaveText(text);
      await ready();
      await expect(page.locator("#chat-error")).toBeHidden();
    };
    const choose = async (id) => {
      await page.locator(`[data-conversation-id="${id}"]`).click();
      await ready();
      assert.equal(await selected(), id);
    };
    await page.goto(origin);
    await login("browser_test");
    assert.equal(await selected(), "web");
    await send("原有网页会话");
    await expect(page.locator("#chat-messages .assistant").last()).toHaveText("收到：原有网页会话");
    await page.locator("#chat-new").click();
    await ready();
    const first = await selected();
    assert.notEqual(first, "web");
    await expect(page.locator("#chat-messages .chat-message")).toHaveCount(0);
    await send("上海酒店查询");
    await send("补充入住日期");
    await expect(page.locator("#chat-messages .assistant")).toHaveText(["收到：上海酒店查询", "收到：补充入住日期"]);
    await expect(page.locator("#conversation-title")).toHaveText("上海酒店查询");
    await page.locator("#chat-input").fill("尚未发送的草稿");
    await page.locator("#chat-new").click();
    await ready();
    const second = await selected();
    assert.notEqual(first, second);
    await expect(page.locator("#chat-input")).toHaveValue("");
    await send("北京机票查询");
    await choose(first);
    await expect(page.locator("#chat-input")).toHaveValue("尚未发送的草稿");
    await expect(page.locator("#chat-messages .user")).toHaveText(["上海酒店查询", "补充入住日期"]);
    await page.reload();
    await expect(page.locator("#chat-input")).toBeEnabled();
    assert.equal(await selected(), first);
    await send("confirm-test");
    await expect(page.locator("#chat-confirm")).toBeVisible();
    const pending = await (await context.request.get(`${origin}/api/conversations/${first}/chat`)).json();
    await choose(second);
    await expect(page.locator("#chat-confirm")).toBeHidden();
    assert.equal((await post(`/api/conversations/${second}/chat`, {
      request_id: randomUUID().replaceAll("-", ""), resume: true, revision: pending.revision,
    })).status(), 409);
    await send("第二个会话继续");
    await choose(first);
    await expect(page.locator("#chat-confirm")).toBeVisible();
    await page.locator("#chat-no").click();
    await ready();
    await expect(page.locator("#chat-messages .assistant").last()).toHaveText("cancelled");
    await choose("web");
    await expect(page.locator("#chat-messages .user")).toHaveText(["原有网页会话"]);
    passed.push("新建、连续发送、历史切换、刷新恢复、草稿隔离、确认隔离、旧会话保留");

    await choose(second);
    // The real server commits the turn, but this client receives no terminal event.
    await page.route("**/chat/stream", async (route) => {
      const response = await route.fetch();
      const text = await response.text();
      assert.ok(text.includes("event: done"));
      await route.fulfill({
        status: 200, contentType: "text/event-stream",
        body: text.slice(0, text.indexOf("event: done")),
      });
    });
    await page.locator("#chat-input").fill("断流恢复验收");
    await page.locator("#chat-submit").click();
    await ready();
    await expect(page.locator("#chat-error")).toBeVisible();
    await expect(page.locator("#chat-submit")).toBeDisabled();
    await expect(page.locator("#chat-input")).toBeDisabled();
    await page.unroute("**/chat/stream");
    await page.locator("#chat-reload").click();
    await ready();
    await expect(page.locator("#chat-input")).toBeEnabled();
    await expect(page.locator("#chat-input")).toHaveValue("");
    await expect(page.locator("#chat-messages .user").filter({ hasText: "断流恢复验收" })).toHaveCount(1);
    await expect(page.locator("#chat-messages .assistant").last()).toHaveText("收到：断流恢复验收");
    passed.push("缺少终态后禁止重发，刷新恢复已提交答案且清空已发送草稿");

    // UI-only status injection; real concurrency and lock loss are tested in PostgreSQL.
    const runRoute = `**/api/conversations/${second}/runs`;
    const runId = randomUUID().replaceAll("-", "");
    await page.route(runRoute, (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ run: { id: runId, status: "running", progress: { text: "正在查询酒店" } } }),
    }));
    await page.reload();
    await expect(page.locator("#chat-status")).toContainText("正在查询酒店");
    await expect(page.locator("#chat-input")).toBeDisabled();
    await expect(page.locator("#chat-reload")).toBeEnabled();
    await page.unroute(runRoute);
    await page.locator("#chat-reload").click();
    await ready();
    await expect(page.locator("#chat-input")).toBeEnabled();
    await expect(page.locator("#chat-messages .assistant").last()).toHaveText("收到：断流恢复验收");
    await page.route(runRoute, (route) => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ run: { id: runId, status: "interrupted", needs_review: true } }),
    }));
    // A status/history disagreement must fail closed, not reopen sending.
    await page.locator("#chat-reload").click();
    await ready();
    await expect(page.locator("#chat-input")).toBeDisabled();
    await page.unroute(runRoute);
    await page.locator("#chat-new").click();
    await ready();
    await expect(page.locator("#chat-input")).toBeEnabled();
    passed.push("运行中刷新可查询不可发送，状态冲突保持阻断，新对话不继承阻断");

    // Restore an older conversation absent from page one of the sidebar.
    await choose(first);
    for (let i = 0; i < 51; i++) assert.equal((await post("/api/conversations")).status(), 201);
    await page.reload();
    await expect(page.locator("#chat-input")).toBeEnabled();
    await expect(page.locator("#conversation-title")).toHaveText("上海酒店查询");
    assert.equal(await selected(), first);
    await page.locator("#conversations-more").click();
    await ready();
    await expect(page.locator("#conversations-more")).toBeHidden();
    await choose(second);
    passed.push("超过一页的会话恢复和分页");

    // Simulate a history failure after successful creation. Sending must stay disabled.
    await page.route("**/api/conversations/*/chat", (route) => route.fulfill({
      status: 503, contentType: "application/json", body: JSON.stringify({ detail: "测试加载失败" }),
    }));
    await page.locator("#chat-new").click();
    await ready();
    await expect(page.locator("#chat-error")).toBeVisible();
    await expect(page.locator("#chat-input")).toBeDisabled();
    await expect(page.locator("#chat-submit")).toBeDisabled();
    await page.unroute("**/api/conversations/*/chat");
    await page.locator("#chat-reload").click();
    await ready();
    await expect(page.locator("#chat-input")).toBeEnabled();
    passed.push("新建后加载失败不会误发至旧会话");

    const other = await browser.newContext();
    assert.equal((await other.request.post(`${origin}/api/auth/login`, {
      headers: { Origin: origin },
      data: { username: "browser_other", password: process.env.AUTH_TEST_PASSWORD },
    })).status(), 200);
    assert.equal((await other.request.get(`${origin}/api/conversations/${first}/chat`)).status(), 404);
    assert.equal((await other.request.post(`${origin}/api/conversations/${first}/chat/stream`, {
      headers: { Origin: origin },
      data: { request_id: randomUUID().replaceAll("-", ""), message: "foreign" },
    })).status(), 404);
    const foreignList = await (await other.request.get(`${origin}/api/conversations`)).json();
    assert.deepEqual(foreignList.items.map((x) => x.id), ["web"]);
    await other.close();
    passed.push("真实登录态下跨用户读取、发送和列表隔离");

    // Reach an older row through pagination before visual checks.
    await page.locator("#conversations-more").click();
    await ready();
    await choose(first);
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.locator("#chat-input").scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      for (const id of ["chat-input", "chat-submit"]) {
        assert.equal(await page.locator(`#${id}`).evaluate((element) => {
          const r = element.getBoundingClientRect();
          return r.width > 30 && r.height >= 40 && r.left >= 0 && r.right <= innerWidth;
        }), true);
      }
      assert.equal(await page.locator(".brand img").evaluate((img) => img.complete && img.naturalWidth > 0), true);
      await page.screenshot({ path: path.join(output, `conversations-${width}.png`), fullPage: true });
    }
    passed.push("桌面、390px和320px布局、输入框可见、品牌资源加载");
    await page.locator("#logout").click();
    await expect(page.locator("#login-view")).toBeVisible();
    await expect(page.locator("#chat-messages .chat-message")).toHaveCount(0);
    assert.equal((await context.request.get(`${origin}/api/conversations`)).status(), 401);
    await login("browser_other");
    assert.equal(await selected(), "web");
    await expect(page.locator("#chat-messages .chat-message")).toHaveCount(0);
    assert.deepEqual(errors, []);
    passed.push("退出和切换账号后无历史消息残留、无JS异常");
    fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify({ passed, errors }, null, 2));
    console.log("PASS:", passed.join("; "));
  } catch (error) {
    fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify({ passed, failure: error.message }, null, 2));
    if (page) await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    throw error;
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
