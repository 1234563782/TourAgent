// Real models and production graph; the harness supplies an isolated random account.
const { chromium, expect } = require("playwright/test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");

(async () => {
  const origin = process.env.AUTH_TEST_ORIGIN;
  const output = process.env.AUTH_TEST_OUTPUT;
  const passed = [];
  const turns = [];
  const errors = [];
  let stage = "launch";
  const record = (next) => {
    stage = next;
    fs.writeFileSync(path.join(output, "progress.json"), JSON.stringify({ stage, passed, turns, errors }, null, 2));
    console.log("STAGE:", stage);
  };
  let browser;
  let page;
  try {
    record("launch");
    browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
    record("context");
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.name));
    const history = async () => {
      const response = await context.request.get(`${origin}/api/conversations/web/chat`);
      assert.equal(response.status(), 200);
      return response.json();
    };
    const send = async (question) => {
      record(`turn-${turns.length + 1}:input`);
      await expect(page.locator("#chat-input")).toBeEnabled();
      await page.locator("#chat-input").fill(question);
      const responsePromise = page.waitForResponse((response) =>
        response.url().endsWith("/chat/stream") && response.request().method() === "POST", { timeout: 90000 });
      await page.locator("#chat-submit").click();
      const response = await responsePromise;
      assert.equal(response.status(), 200);
      record(`turn-${turns.length + 1}:stream`);
      await expect(page.locator("#chat-input")).toBeEnabled({ timeout: 90000 });
      await expect(page.locator("#chat-error")).toBeHidden();
      record(`turn-${turns.length + 1}:history`);
      const state = await history();
      const answer = await page.locator("#chat-messages .assistant").last().innerText();
      turns.push({ question, answer, tasks: state.tasks.map(({ intent, status, attempt, missing_fields }) =>
        ({ intent, status, attempt, missing_fields })) });
      record(`turn-${turns.length}:done`);
      return state;
    };
    record("login");
    await page.goto(origin);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.locator("#login-submit").click();
    await expect(page.locator("#workspace")).toBeVisible();

    const first = await send("帮我查青岛的酒店，入住和退房日期还没有确定。");
    const task = first.tasks.find((item) => item.intent === "plan_trip");
    assert.ok(task);
    assert.equal(task.status, "needs_clarification");
    assert.deepEqual([...task.missing_fields].sort(), ["check_in", "check_out"]);
    assert.match(turns.at(-1).answer, /入住/);
    assert.match(turns.at(-1).answer, /退房/);
    passed.push("缺日期的酒店任务进入待补充并追问");

    const resumed = await send("继续刚才青岛酒店的查询任务，日期仍然没确定。");
    const resumedTask = resumed.tasks.find((item) => item.id === task.id);
    assert.equal(resumedTask.status, "needs_clarification");
    assert.equal(resumedTask.attempt, task.attempt + 1);
    assert.equal(resumed.tasks.filter((item) => item.intent === "plan_trip").length, 1);
    passed.push("明确继续复用原任务，仍缺字段不虚构结果");

    for (const question of [
      "先别取消酒店查询，我还在考虑日期。",
      "保持现在的待办就好，暂时不需要重新查。",
      "如果行程有变我会再通知，现在先不做任何操作。",
    ]) {
      const negated = await send(question);
      assert.equal(negated.tasks.find((item) => item.id === task.id).status, "needs_clarification");
      assert.equal(negated.tasks.find((item) => item.id === task.id).attempt, resumedTask.attempt);
      assert.doesNotMatch(turns.at(-1).answer, /信息提取未通过|模型服务不可用|稍后重试/);
    }
    passed.push("否定取消不会取消待办");

    const cancelled = await send("取消刚才青岛酒店的查询任务，不用继续找了。");
    assert.equal(cancelled.tasks.find((item) => item.id === task.id).status, "cancelled");
    assert.match(turns.at(-1).answer, /已取消/);
    passed.push("明确取消返回中文结果并持久化终态");

    await page.reload();
    await expect(page.locator("#chat-input")).toBeEnabled();
    assert.equal((await history()).tasks.find((item) => item.id === task.id).status, "cancelled");
    await expect(page.locator("#chat-messages .user")).toHaveCount(turns.length);
    const greeted = await send("你好");
    assert.equal(greeted.tasks.find((item) => item.id === task.id).status, "cancelled");
    passed.push("整页刷新和后续闲聊不复活取消任务");

    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.locator("#chat-input").scrollIntoViewIfNeeded();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await expect(page.locator("#chat-input")).toBeVisible();
      const box = await page.locator("#chat-input").boundingBox();
      assert.ok(box && box.width > 100 && box.height >= 40 && box.x >= 0 && box.x + box.width <= width);
      await page.screenshot({ path: path.join(output, `task-controls-${width}.png`), fullPage: true });
    }
    assert.deepEqual(errors, []);
    passed.push("桌面和移动端输入框可见、无横向溢出、无JS异常");
    fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify({ passed, turns, errors }, null, 2));
    console.log("PASS:", passed.join("; "));
  } catch (error) {
    fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify({
      stage, passed, turns, errors, failure: error.message,
    }, null, 2));
    if (page) await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    throw error;
  } finally {
    if (browser) await browser.close();
  }
})().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
