// Isolated accounts and real admission SQL; no paid provider calls.
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
    await page.goto(origin);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.locator("#login-submit").click();
    await expect(page.locator("#chat-input")).toBeEnabled();
    await page.locator("#chat-input").fill("合成正常请求");
    await page.locator("#chat-submit").click();
    await expect(page.locator("#chat-messages .assistant").last()).toHaveText("收到：合成正常请求");
    await expect(page.locator("#chat-new")).toBeEnabled();
    passed.push("真实账号与数据库执行首条请求");

    const responsePromise = page.waitForResponse((response) =>
      response.url().endsWith("/chat/stream") && response.status() === 429);
    await page.locator("#chat-input").fill("保留这条未执行的草稿");
    await page.locator("#chat-submit").click();
    const response = await responsePromise;
    assert.equal(response.headers()["x-travel-admission"], "rejected");
    assert.ok(Number(response.headers()["retry-after"]) > 0);
    await expect(page.locator("#chat-error")).toContainText("本次尚未执行");
    await expect(page.locator("#chat-input")).toBeEnabled();
    await expect(page.locator("#chat-submit")).toBeEnabled();
    await expect(page.locator("#chat-input")).toHaveValue("保留这条未执行的草稿");
    await expect(page.locator("#chat-messages .user")).toHaveCount(1);
    await expect(page.locator("#chat-messages .assistant")).toHaveCount(1);
    passed.push("配额拒绝返回429及重试头，保留草稿、删除临时气泡、不锁死输入");

    const before = await (await context.request.get(`${origin}/api/conversations/web/runs`)).json();
    assert.equal(before.run.status, "completed");
    await page.locator("#chat-reload").click();
    await expect(page.locator("#chat-new")).toBeEnabled();
    await expect(page.locator("#chat-input")).toBeEnabled();
    await expect(page.locator("#chat-input")).toHaveValue("保留这条未执行的草稿");
    passed.push("拒绝不覆盖已完成运行，刷新仍可编辑未发送草稿");

    await page.locator("#chat-new").click();
    await expect(page.locator("#chat-new")).toBeEnabled();
    await page.locator("#chat-input").fill("新会话也不能绕过个人配额");
    await page.locator("#chat-submit").click();
    await expect(page.locator("#chat-error")).toContainText("本次尚未执行");
    await expect(page.locator("#chat-input")).toBeEnabled();
    await expect(page.locator("#chat-messages .chat-message")).toHaveCount(0);
    const selected = await page.locator('#conversation-list [aria-current="true"]').getAttribute("data-conversation-id");
    assert.equal((await (await context.request.get(`${origin}/api/conversations/${selected}/runs`)).json()).run, null);
    passed.push("新建会话不能绕过配额，拒绝后没有failed运行");

    const other = await browser.newContext();
    assert.equal((await other.request.post(`${origin}/api/auth/login`, {
      headers: { Origin: origin },
      data: { username: "browser_other", password: process.env.AUTH_TEST_PASSWORD },
    })).status(), 200);
    assert.equal((await other.request.post(`${origin}/api/chat`, {
      headers: { Origin: origin },
      data: { request_id: randomUUID().replaceAll("-", ""), message: "另一用户独立额度" },
    })).status(), 200);
    await other.close();
    passed.push("另一真实账号的个人额度不受影响");

    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.locator("#chat-input").scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: path.join(output, `resources-${width}.png`), fullPage: true });
    }
    assert.deepEqual(errors, []);
    passed.push("桌面及手机无横向溢出，无JS异常");
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
