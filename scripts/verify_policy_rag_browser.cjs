// Real model answers against the published simulated policy; no fixture responses.
const { chromium, expect: baseExpect } = require("playwright/test");
const expect = baseExpect.configure({ timeout: 90000 });
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const origin = process.env.AUTH_TEST_ORIGIN;
  const output = process.env.AUTH_TEST_OUTPUT;
  const report = { passed: [], failed: [], turns: [], errors: [] };
  const save = () => fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(report, null, 2));
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    page = await context.newPage();
    page.on("pageerror", (error) => report.errors.push(error.name));
    page.setDefaultTimeout(150000);
    await page.goto(origin);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.locator("#login-submit").click();
    await expect(page.locator("#workspace")).toBeVisible();

    let explanationCount = 0;
    const send = async (question, fresh = false) => {
      if (fresh) {
        await page.locator("#chat-new").click();
        await expect(page.locator("#chat-messages .user")).toHaveCount(0);
      }
      await expect(page.locator("#chat-input")).toBeEnabled();
      report.stage = `question-${report.turns.length + 1}`;
      const started = Date.now();
      console.log("STAGE:", report.stage);
      save();
      await page.locator("#chat-input").fill(question);
      const pending = page.waitForResponse((r) =>
        r.url().endsWith("/chat/stream") && r.request().method() === "POST");
      await page.locator("#chat-submit").click();
      const response = await pending;
      assert.equal(response.status(), 200);
      await expect(page.locator("#chat-input")).toBeEnabled();
      await expect(page.locator("#chat-error")).toBeHidden();
      const historyUrl = response.url().replace(/\/stream$/, "");
      const history = await context.request.get(historyUrl);
      assert.equal(history.status(), 200);
      const state = await history.json();
      const answer = await page.locator("#chat-messages .assistant").last().innerText();
      const diagnostics = path.join(output, "policy-explanations.json");
      const explanations = fs.existsSync(diagnostics) ? JSON.parse(fs.readFileSync(diagnostics, "utf8")) : [];
      const policyRequest = explanations.length > explanationCount ? explanations.at(-1).test_request : null;
      explanationCount = explanations.length;
      report.turns.push({ question, answer, elapsed_ms: Date.now() - started,
        policy_request: policyRequest, tasks: state.tasks });
      save();
      return { answer, state, policyRequest };
    };
    const check = async (name, action) => {
      try {
        await action();
        report.passed.push(name);
      } catch (error) {
        report.failed.push({ name, reason: error.message });
      }
      save();
    };
    const cited = (text) => {
      assert.match(text, /\[S\d+\]/);
      assert.match(text, /V1\.1/);
      assert.doesNotMatch(text, /信息提取未通过|暂不可用|依据不完整|证据不足/);
    };
    const first = await send("请解释当前差旅制度：住宿费是按每人每天还是每间每晚计算？含税吗？限额是不是固定发放？只讲制度，不核验个人费用。");
    await check("计价单位、税费及实报条件有引用", () => {
      cited(first.answer);
      assert.match(first.answer, /每间每晚/);
      assert.match(first.answer, /含税/);
      assert.match(first.answer, /实际|实报/);
    });
    const second = await send("按刚才的制度，普通员工在上海单人入住，住宿上限是多少？只解释标准，不查酒店。");
    await check("多轮承接及表格金额420", () => {
      cited(second.answer);
      assert.match(second.answer, /420/);
      assert.match(second.answer, /每间每晚|元\/间\/晚/);
    });
    const third = await send("请解释当前住宿制度：两晚房费一晚超限、一晚低于限额，能不能平均后报销？不能的话是否存在特殊审批口径？不需要算个人费用。", true);
    await check("逐晚判断且保留专项审批例外", () => {
      cited(third.answer);
      assert.match(third.answer, /逐晚|每晚/);
      assert.match(third.answer, /不.*平均|不能.*冲抵|不得.*冲抵/);
      assert.match(third.answer, /专项审批|特批/);
    });
    const missing = await send("我在2026-09-14入住，请根据当前制度告诉我出差住宿每晚的限额，我还没说明目的地和职级。", true);
    const originalTask = missing.state.tasks.find((t) => t.intent === "policy_explain");
    await check("缺城市职级时追问而非猜金额", () => {
      assert.match(missing.answer, /目的地|城市|地区/);
      assert.match(missing.answer, /职级|岗位/);
      assert.doesNotMatch(missing.answer, /信息提取未通过|暂不可用/);
      assert.equal(originalTask?.status, "needs_clarification");
      assert.equal(missing.policyRequest?.facts.expense_date, "2026-09-14");
    });
    const smalltalk = await send("谢谢你，我先想一想。");
    await check("闲聊不取消或执行待补充任务", () => {
      assert.deepEqual(smalltalk.state.tasks.find((t) => t.id === originalTask?.id), originalTask);
      assert.equal(smalltalk.policyRequest, null);
      assert.doesNotMatch(smalltalk.answer, /信息提取未通过|暂不可用/);
    });
    const continuedTask = (turn, prior, status, anchor = originalTask) => {
      const task = turn.state.tasks.find((t) => t.id === anchor?.id);
      assert.ok(task, "应继续原任务而非新增一个不相关任务");
      assert.equal(task.subject_id, anchor.subject_id);
      assert.equal(task.status, status);
      assert.equal(task.revision, prior.revision + 1);
      assert.equal(task.attempt, prior.attempt + 1);
      assert.equal(turn.state.tasks.filter((t) => t.intent === "policy_explain").length, 1);
      return task;
    };
    const city = await send("目的地是武汉，单人入住。");
    await check("只补城市仍追问职级且保持原任务", () => {
      continuedTask(city, originalTask, "needs_clarification");
      assert.match(city.answer, /职级|岗位/);
      assert.doesNotMatch(city.answer, /信息提取未通过|暂不可用/);
    });
    const grade = await send("我是普通员工。");
    await check("补齐职级后完成原任务并给出340标准", () => {
      continuedTask(grade, city.state.tasks.find((t) => t.id === originalTask?.id), "completed");
      cited(grade.answer);
      assert.match(grade.answer, /340/);
      assert.match(grade.answer, /每间每晚|元\/间\/晚/);
      assert.equal(grade.policyRequest?.facts.expense_date, "2026-09-14");
    });
    const changed = await send("目的地改成杭州，其他条件不变，住宿上限重新看一下。");
    await check("更换城市重算为420并使旧结果失效", () => {
      continuedTask(changed, grade.state.tasks.find((t) => t.id === originalTask?.id), "completed");
      cited(changed.answer);
      assert.match(changed.answer, /420/);
      assert.equal(changed.policyRequest?.facts.destination, "杭州");
      assert.equal(changed.policyRequest?.facts.employee_grade, "普通员工");
      assert.equal(changed.policyRequest?.facts.expense_date, "2026-09-14");
      const task = changed.state.tasks.find((t) => t.id === originalTask?.id);
      assert.notDeepEqual(task.result_ref, grade.state.tasks.find((t) => t.id === originalTask?.id)?.result_ref);
    });
    const historic = await send("实际入住日期改为2026-09-01，仍按当前采用的制度解释，其他条件不变。");
    await check("实际入住日期不切换当前政策版本", () => {
      continuedTask(historic, changed.state.tasks.find((t) => t.id === originalTask?.id), "completed");
      cited(historic.answer);
      assert.match(historic.answer, /420/);
      assert.equal(historic.policyRequest?.facts.expense_date, "2026-09-01");
    });
    const restored = await send("实际入住日期改回2026-09-14，重新解释刚才的住宿上限。");
    await check("更正实际日期后重新完成原任务", () => {
      continuedTask(restored, historic.state.tasks.find((t) => t.id === originalTask?.id), "completed");
      cited(restored.answer);
      assert.match(restored.answer, /420/);
    });
    const promotion = await send("职级改为部门经理，其他条件不变，重新解释住宿标准。");
    await check("更换职级重新解释为500且不新增事项", () => {
      continuedTask(promotion, restored.state.tasks.find((t) => t.id === originalTask?.id), "completed");
      cited(promotion.answer);
      assert.match(promotion.answer, /500/);
      assert.equal(promotion.policyRequest?.facts.destination, "杭州");
      assert.equal(promotion.policyRequest?.facts.expense_date, "2026-09-14");
    });
    const isolated = await send("按当前制度，我的住宿每晚最多能报多少？", true);
    await check("新会话不继承上一会话城市职级", () => {
      const task = isolated.state.tasks.find((t) => t.intent === "policy_explain");
      assert.equal(task?.status, "needs_clarification");
      assert.notEqual(task.subject_id, originalTask?.subject_id);
      assert.match(isolated.answer, /城市|目的地/);
      assert.match(isolated.answer, /职级|岗位/);
      assert.doesNotMatch(isolated.answer, /信息提取未通过|暂不可用/);
    });
    const undated = await send("按2026年的公司制度，住宿按什么单位计价？只讲制度，出差日期未定。", true);
    await check("一般咨询不把政策年份当费用日期", () => {
      cited(undated.answer);
      assert.match(undated.answer, /每间每晚/);
      assert.ok(undated.policyRequest);
      assert.equal(undated.policyRequest.facts.expense_date ?? null, null);
      assert.doesNotMatch(undated.answer, /请.*日期|政策适用日期：/);
    });
    const outside = await send("请解释当前差旅制度是否规定了境外出差住宿金额，能否直接套用大陆城市的限额？只按文件说明。", true);
    await check("境外不套用大陆金额并注明来源", () => {
      cited(outside.answer);
      assert.match(outside.answer, /境外/);
      assert.match(outside.answer, /不套用|不能.*套用|不适用|不可.*套用/);
    });
    const alternative = await send("主管去成都，2026-09-09入住，单人住宿标准是什么？只解释当前制度。", true);
    const alternativeTask = alternative.state.tasks.find((t) => t.intent === "policy_explain");
    await check("另一组条件和非当天日期给出420", () => {
      assert.equal(alternativeTask?.status, "completed");
      cited(alternative.answer);
      assert.match(alternative.answer, /420/);
      assert.ok(!Object.hasOwn(alternative.policyRequest, "as_of"));
      assert.equal(alternative.policyRequest?.facts.expense_date, "2026-09-09");
    });
    const alternativeCity = await send("去的地方改成苏州了，别的照旧。");
    await check("口语更改地点后保留入住日期并更新为500", () => {
      continuedTask(alternativeCity, alternativeTask, "completed", alternativeTask);
      cited(alternativeCity.answer);
      assert.match(alternativeCity.answer, /500/);
      assert.equal(alternativeCity.policyRequest?.facts.expense_date, "2026-09-09");
      assert.equal(alternativeCity.policyRequest?.facts.destination, "苏州");
    });
    const alternativeGrade = await send("更正一下，是普通员工，不是主管。");
    await check("带否定的职级更正更新为420且日期不漂移", () => {
      continuedTask(alternativeGrade,
        alternativeCity.state.tasks.find((t) => t.id === alternativeTask?.id), "completed", alternativeTask);
      cited(alternativeGrade.answer);
      assert.match(alternativeGrade.answer, /420/);
      assert.equal(alternativeGrade.policyRequest?.facts.expense_date, "2026-09-09");
      assert.equal(alternativeGrade.policyRequest?.facts.employee_grade, "普通员工");
    });
    const switched = await send("现在不解释标准了，请核验刚才这笔实际住宿费用能否报销，金额是400元。");
    await check("显式切换核验保留任务ID并按缺失事实追问", () => {
      const task = switched.state.tasks.find((t) => t.id === alternativeTask?.id);
      const prior = alternativeGrade.state.tasks.find((t) => t.id === alternativeTask?.id);
      assert.equal(task?.intent, "policy_check");
      assert.equal(task?.operation, "change_intent");
      assert.equal(task?.revision, prior.revision + 1);
      assert.equal(task?.status, "needs_clarification");
      assert.equal(switched.policyRequest, null);
      assert.match(switched.answer, /请补充|核实/);
    });
    const back = await send("不核验这笔费用了，改成只解释刚才条件下的住宿限额。");
    await check("转回解释保持任务及原入住日期", () => {
      const task = back.state.tasks.find((t) => t.id === alternativeTask?.id);
      assert.equal(task?.intent, "policy_explain");
      assert.equal(task?.operation, "change_intent");
      assert.equal(task?.status, "completed");
      cited(back.answer);
      assert.equal(back.policyRequest?.facts.expense_date, "2026-09-09");
    });
    const withdrawn = await send("刚才说的入住日期不确定，撤回那个日期，暂时不要默认按今天，仍解释住宿限额。");
    await check("无季节差价时撤回日期仍可咨询且不默认今天", () => {
      const task = withdrawn.state.tasks.find((t) => t.id === alternativeTask?.id);
      assert.equal(task?.status, "completed");
      assert.ok(withdrawn.policyRequest);
      assert.equal(withdrawn.policyRequest.facts.expense_date ?? null, null);
      cited(withdrawn.answer);
      assert.match(withdrawn.answer, /420/);
      assert.doesNotMatch(withdrawn.answer, /请.*日期|政策适用日期：/);
    });
    const dated = await send("实际入住日期确定为2026-09-10，解释住宿限额，其他条件不变。");
    await check("补回实际日期后仍使用当前政策", () => {
      const task = dated.state.tasks.find((t) => t.id === alternativeTask?.id);
      assert.equal(task?.status, "completed");
      assert.equal(dated.policyRequest?.facts.expense_date, "2026-09-10");
      cited(dated.answer);
      assert.doesNotMatch(dated.answer, /政策适用日期：/);
    });
    await check("刷新恢复真实回答", async () => {
      await page.reload();
      await expect(page.locator("#chat-input")).toBeEnabled();
      assert.equal(await page.locator("#chat-messages .assistant").last().innerText(), dated.answer);
    });
    await check("桌面移动端可见且无横向溢出", async () => {
      for (const width of [1440, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        await page.locator("#chat-input").scrollIntoViewIfNeeded();
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        const box = await page.locator("#chat-input").boundingBox();
        assert.ok(box && box.width > 100 && box.height >= 40 && box.x >= 0 && box.x + box.width <= width);
        await page.screenshot({ path: path.join(output, `policy-rag-${width}.png`), fullPage: true });
      }
      assert.deepEqual(report.errors, []);
    });
    report.stage = "finished";
    save();
    console.log(JSON.stringify({ passed: report.passed.length, failed: report.failed.length }));
    if (report.failed.length) process.exitCode = 1;
  } catch (error) {
    report.failed.push({ name: report.stage || "browser-startup",
      reason: String(error.message).replaceAll(process.env.AUTH_TEST_PASSWORD, "[REDACTED]").slice(0, 3000) });
    save();
    if (page) await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true });
    process.exitCode = 1;
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.name);
  process.exitCode = 1;
});
