const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  const report = { cases: [], passed: false };
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    page.setDefaultTimeout(120000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.name));
    await page.goto(process.env.AUTH_TEST_ORIGIN);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.locator("#workspace").waitFor({ state: "visible" });
    await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
    const send = async (question) => {
      await page.getByLabel("消息", { exact: true }).fill(question);
      const result = page.waitForResponse((response) =>
        response.url().endsWith("/api/chat") && response.request().method() === "POST");
      await page.getByRole("button", { name: "发送", exact: true }).click();
      const response = await result;
      const body = await response.json();
      const answer = body.messages?.filter((item) => item.role === "assistant").at(-1)?.text;
      report.cases.push({ question, status: response.status(), answer: answer || null });
      assert.equal(response.status(), 200);
      assert.ok(answer?.length);
      assert.doesNotMatch(answer, /本次信息提取未通过|模型服务不可用|未取得业务结果/);
      await page.waitForFunction(() => !document.getElementById("chat-submit").disabled);
      return answer;
    };
    await send("我下周一从北京去上海出差，帮我规划交通和住宿。");
    const second = await send("上一条我说的是从哪里去哪里？请简短复述。");
    assert.match(second, /北京/);
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll(".chat-message").length === 4);
    assert.equal(await page.locator(".chat-message.assistant").last().textContent(), second);
    assert.equal(await page.locator(".brand img").evaluate((img) => img.complete && img.naturalWidth > 0), true);
    await page.screenshot({ path: path.join(process.env.AUTH_TEST_OUTPUT, "chat-desktop.png"), fullPage: true });
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      const inputBox = await page.locator("#chat-input").boundingBox();
      const buttonBox = await page.locator("#chat-submit").boundingBox();
      assert.ok(inputBox.width > 100 && buttonBox.x >= inputBox.x + inputBox.width);
      await page.screenshot({ path: path.join(process.env.AUTH_TEST_OUTPUT, `chat-${width}.png`), fullPage: true });
    }
    await page.getByRole("button", { name: "退出登录", exact: true }).click();
    await page.locator("#login-view").waitFor({ state: "visible" });
    assert.equal(await page.locator(".chat-message").count(), 0);
    assert.equal((await context.request.get(process.env.AUTH_TEST_ORIGIN + "/api/chat")).status(), 401);
    assert.deepEqual(errors, []);
    report.passed = true;
    console.log("PASS: real model chat, contextual follow-up, reload, logout, desktop/mobile");
  } finally {
    fs.writeFileSync(path.join(process.env.AUTH_TEST_OUTPUT, "results.json"),
      JSON.stringify(report, null, 2), "utf8");
    await browser.close();
  }
})().catch((error) => {
  console.error("Chat browser verification failed:", error.name);
  process.exitCode = 1;
});
