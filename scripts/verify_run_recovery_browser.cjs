// 使用真实页面、鉴权、HTTP 和 PostgreSQL；执行故障由隔离测试图注入。
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  const report = { passed: false, checks: [], realModel: false };
  const output = process.env.AUTH_TEST_OUTPUT;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(process.env.AUTH_TEST_ORIGIN);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
    const send = async (text) => {
      await page.getByLabel("消息", { exact: true }).fill(text);
      const response = page.waitForResponse((r) => r.url().endsWith("/chat/stream"));
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await response;
      await page.waitForFunction(() => !document.getElementById("chat-reload").disabled);
    };
    await send("完整的背景信息");
    await send("会在保存后失败的请求");
    assert.equal(await page.locator("#chat-submit").isDisabled(), true);
    await page.getByRole("button", { name: "刷新会话", exact: true }).click();
    await page.locator("#chat-recover").waitFor({ state: "visible" });
    assert.equal(await page.locator("#chat-submit").isDisabled(), true);
    await page.getByRole("button", { name: "核实并恢复", exact: true }).click();
    await page.locator("#recovery-dialog").waitFor({ state: "visible" });
    assert.match(await page.locator("#recovery-trips").textContent(), /合成故障前已保存的草案/);
    assert.equal(await page.locator("#recovery-submit").isDisabled(), true);
    report.checks.push("保存后失败仍禁止发送，核实页显示已持久化草案，未勾选不能确认");
    const capture = async (width) => {
      await page.setViewportSize({ width, height: 900 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const box = await page.locator("#recovery-dialog").boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width);
      await page.locator("#recovery-submit").scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, `recovery-${width}.png`), fullPage: true });
    };
    for (const width of [1440, 390, 320]) await capture(width);
    report.checks.push("1440、390、320宽度弹窗和确认按钮可见，无横向溢出");
    await page.locator("#recovery-reviewed").check();
    // Simulate a lost response after the server commits the fork, not a failed write.
    let committedTarget = null;
    await page.route("**/recovery", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      committedTarget = (await response.json()).conversation_id;
      await route.abort("failed");
    });
    await page.locator("#recovery-submit").click();
    await page.locator("#recovery-error").waitFor({ state: "visible" });
    assert.ok(committedTarget);
    await page.unroute("**/recovery");
    await page.locator("#recovery-cancel").click();
    await page.locator("#chat-recover").click();
    await page.getByRole("button", { name: "打开恢复对话", exact: true }).waitFor();
    await page.locator("#recovery-submit").click();
    await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
    assert.match(await page.locator("#chat-messages").textContent(), /完整的背景信息/);
    assert.doesNotMatch(await page.locator("#chat-messages").textContent(), /保存后失败/);
    assert.equal(await page.locator(`#conversation-list button[data-conversation-id="${committedTarget}"]`)
      .getAttribute("aria-current"), "true");
    const memory = await context.request.get(`${process.env.AUTH_TEST_ORIGIN}/api/memory`);
    assert.equal((await memory.json()).trip_history.length, 1);
    report.checks.push("确认响应丢失后打开同一恢复对话，未重复建会话或写行程，完整历史保留");
    await send("新的请求");
    assert.match(await page.locator("#chat-messages").textContent(), /合成回答：新的请求/);
    const restoredChat = page.waitForResponse((response) =>
      response.url().endsWith(`/api/conversations/${committedTarget}/chat`));
    await page.reload();
    assert.equal((await restoredChat).status(), 200);
    await page.locator("#workspace").waitFor({ state: "visible" });
    await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
    assert.equal(await page.locator(`#conversation-list button[data-conversation-id="${committedTarget}"]`)
      .getAttribute("aria-current"), "true");
    assert.match(await page.locator("#chat-messages").textContent(), /新的请求/);
    report.checks.push("恢复会话可发送新请求，刷新后历史仍在");
    await page.getByRole("button", { name: "退出登录", exact: true }).click();
    await page.locator("#login-view").waitFor({ state: "visible" });
    assert.equal(await page.locator("#recovery-trips").textContent(), "");
    assert.equal((await context.request.get(`${process.env.AUTH_TEST_ORIGIN}/api/conversations/web/recovery`)).status(), 401);
    assert.deepEqual(errors, []);
    report.checks.push("退出清除核实内容，匿名恢复接口401，页面无JS异常");
    report.passed = true;
    console.log("PASS: recovery browser, persisted effects, lost response, desktop/mobile");
  } finally {
    fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(report, null, 2), "utf8");
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
