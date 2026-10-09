// Real UI, model and PostgreSQL. Only designated model outages are synthetic.
const { chromium, expect: baseExpect } = require("playwright/test");
const expect = baseExpect.configure({ timeout: 150000 });
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const output = process.env.AUTH_TEST_OUTPUT;
  const report = { passed: [], failed: [], turns: [], errors: [] };
  const save = () => fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(report, null, 2));
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    page = await context.newPage();
    page.setDefaultTimeout(150000);
    page.on("pageerror", (error) => report.errors.push(error.name));
    await page.goto(process.env.AUTH_TEST_ORIGIN);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.locator("#login-submit").click();
    await expect(page.locator("#workspace")).toBeVisible();
    const send = async (question, injected = false) => {
      console.log("STAGE:", report.turns.length + 1);
      await expect(page.locator("#chat-input")).toBeEnabled();
      await page.locator("#chat-input").fill(question);
      const pending = page.waitForResponse((r) =>
        r.url().endsWith("/chat/stream") && r.request().method() === "POST");
      await page.locator("#chat-submit").click();
      const response = await pending;
      assert.equal(response.status(), 200);
      await expect(page.locator("#chat-input")).toBeEnabled();
      await expect(page.locator("#chat-error")).toBeHidden();
      const evidence = JSON.parse(fs.readFileSync(path.join(output, "recovery-events.json"), "utf8"));
      const checkpoint = evidence.checkpoints.at(-1);
      const turn = {
        question, answer: await page.locator("#chat-messages .assistant").last().innerText(),
        checkpoint, checks: evidence.checks, injected: evidence.failures,
      };
      report.turns.push(turn);
      save();
      assert.equal(checkpoint.turn, report.turns.length);
      if (injected) {
        assert.equal(evidence.failures.at(-1)?.turn, report.turns.length, "必须确实注入本轮故障");
        assert.ok(checkpoint.slot_error);
      } else {
        assert.equal(checkpoint.slot_error, null, "非注入轮未通过，停止依赖轮次");
      }
      return turn;
    };
    const checked = (name, action) => {
      action();
      report.passed.push(name);
      save();
    };
    const now = new Date();
    now.setDate(now.getDate() - 1);
    const expenseDate = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"),
      String(now.getDate()).padStart(2, "0")].join("-");
    const first = await send(`核验一笔已经发生的住宿费用：${expenseDate}在天津，普通员工，中国大陆因公出差共2天，没有专项审批，1人单独住一间，实际支付人民币137元，是每间每晚含税价格，日期和账单已核实。`);
    checked("初始事实正常提交并核验一次", () => {
      assert.equal(first.checkpoint.slots.expense.values.amount, "137");
      assert.equal(first.checks.length, 1);
    });
    const failed = await send("刚才那笔住宿费金额改为181元，其他条件不变，请重新核验。", true);
    checked("注入故障后只保存待处理原话，不提交补丁或执行核验", () => {
      assert.deepEqual(failed.checkpoint.slots, first.checkpoint.slots);
      assert.deepEqual(failed.checkpoint.tasks, first.checkpoint.tasks);
      assert.equal(failed.checks.length, 1);
      assert.equal(failed.checkpoint.pending_turns[0].user_message, 1);
      assert.match(failed.answer, /待处理/);
    });
    const chat = await send("先不要重试，打个招呼就行。");
    checked("明确不重试的闲聊不修改事实、不清除失败原话", () => {
      assert.deepEqual(chat.checkpoint.slots, first.checkpoint.slots);
      assert.deepEqual(chat.checkpoint.pending_turns, failed.checkpoint.pending_turns);
      assert.equal(chat.checks.length, 1);
    });
    const retried = await send("现在重试刚才没有保存的金额更正，仍然核验同一笔住宿费。");
    checked("真实模型恢复同一事项、更正有原始引用、只追加一次核验", () => {
      assert.equal(retried.checkpoint.slots.expense.id, first.checkpoint.slots.expense.id);
      assert.equal(retried.checkpoint.slots.expense.revision, first.checkpoint.slots.expense.revision + 1);
      assert.equal(retried.checkpoint.slots.expense.values.amount, "181");
      assert.equal(retried.checkpoint.slots.expense.evidence.amount.user_message, 1);
      assert.equal(retried.checkpoint.pending_turns.length, 0);
      assert.equal(retried.checks.length, 2);
      assert.equal(retried.checks[1].facts.amount, "181");
    });
    const failedCity = await send("刚才那笔实际住宿费的城市改成苏州，其他不变，重新核验。", true);
    checked("第二个不同字段的故障同样不提交", () => {
      assert.deepEqual(failedCity.checkpoint.slots, retried.checkpoint.slots);
      assert.equal(failedCity.checks.length, 2);
    });
    const discard = await send("放弃刚才失败的城市更正，保留已经保存的费用，不要重新核验。");
    checked("明确放弃只清待处理标记、不撤销已保存事实", () => {
      assert.equal(discard.checkpoint.pending_turns.length, 0);
      assert.deepEqual(discard.checkpoint.slots, retried.checkpoint.slots);
      assert.equal(discard.checks.length, 2);
    });
    await page.reload();
    await expect(page.locator("#chat-input")).toBeEnabled();
    checked("刷新后保留最终回答且无浏览器脚本错误", () => assert.deepEqual(report.errors, []));
    await expect(page.locator("#chat-messages .assistant").last()).toHaveText(discard.answer);
    await page.screenshot({ path: path.join(output, "recovery-1440.png"), fullPage: true });
  } catch (error) {
    // Test assertions contain only synthetic user data, never authentication headers.
    report.failed.push({ name: "恢复闭环", reason: error.message });
    if (page) await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
  } finally {
    save();
    await browser.close();
  }
  console.log(JSON.stringify({ passed: report.passed.length, failed: report.failed.length }));
  process.exitCode = report.failed.length ? 1 : 0;
})();
