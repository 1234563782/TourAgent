// Real supplier and model workflow. Assertions inspect requests, not just prose.
const { chromium, expect: baseExpect } = require("playwright/test");
const expect = baseExpect.configure({ timeout: 150000 });
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

(async () => {
  const origin = process.env.AUTH_TEST_ORIGIN;
  const output = process.env.AUTH_TEST_OUTPUT;
  const report = { passed: [], failed: [], turns: [], errors: [] };
  const save = () => fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(report, null, 2));
  const day = (offset) => {
    const current = new Date();
    current.setDate(current.getDate() + offset);
    return [current.getFullYear(), String(current.getMonth() + 1).padStart(2, "0"),
      String(current.getDate()).padStart(2, "0")].join("-");
  };
  const dates = { checkIn: day(7), checkOut: day(8), changedIn: day(9), changedOut: day(10),
    expense: day(-1) };
  report.dates = dates;
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  let page;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    page = await context.newPage();
    page.setDefaultTimeout(150000);
    page.on("pageerror", (error) => report.errors.push(error.name));
    await page.goto(origin);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.locator("#login-submit").click();
    await expect(page.locator("#workspace")).toBeVisible();
    let eventCount = 0;
    const send = async (question) => {
      report.stage = `question-${report.turns.length + 1}`;
      save();
      console.log("STAGE:", report.stage);
      await expect(page.locator("#chat-input")).toBeEnabled();
      await page.locator("#chat-input").fill(question);
      const pending = page.waitForResponse((r) =>
        r.url().endsWith("/chat/stream") && r.request().method() === "POST");
      const started = Date.now();
      await page.locator("#chat-submit").click();
      const response = await pending;
      assert.equal(response.status(), 200);
      await expect(page.locator("#chat-input")).toBeEnabled();
      await expect(page.locator("#chat-error")).toBeHidden();
      const history = await context.request.get(response.url().replace(/\/stream$/, ""));
      assert.equal(history.status(), 200);
      const state = await history.json();
      const file = path.join(output, "hotel-events.json");
      const allEvents = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : [];
      const turn = { question, answer: await page.locator("#chat-messages .assistant").last().innerText(),
        elapsed_ms: Date.now() - started, tasks: state.tasks, events: allEvents.slice(eventCount) };
      eventCount = allEvents.length;
      report.turns.push(turn);
      save();
      const extractionFile = path.join(output, "extraction.json");
      const extraction = fs.existsSync(extractionFile)
        ? JSON.parse(fs.readFileSync(extractionFile, "utf8")).at(-1) : null;
      if (extraction?.reason === "slot_model_unavailable" ||
          extraction?.reason === "semantic_review_unavailable") {
        throw new Error(`Model service failed: ${extraction.error_category}; HTTP ${
          extraction.http_status ?? "unknown"}. Dependent turns not executed.`);
      }
      return turn;
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
    const healthy = (turn) => assert.doesNotMatch(turn.answer,
      /信息提取未通过|模型服务不可用|未取得业务结果|Tool failed/);
    const searches = (turn) => turn.events.filter((e) => e.stage === "search");
    const plan = (turn) => turn.events.findLast((e) => e.stage === "plan")?.result;
    const savedPlan = (turn) => turn.events.findLast((e) =>
      e.stage === "business_result" && e.tool === "plan_trip")?.result.draft;
    const assertRead = (turn, previous, expectedTask, expectedResult) => {
      healthy(turn);
      for (const task of previous.tasks) {
        assert.deepEqual(turn.tasks.find((t) => t.id === task.id), task,
          "只读追问不能修改任何已有任务");
      }
      const created = turn.tasks.filter((t) => !previous.tasks.some((p) => p.id === t.id));
      assert.equal(created.length, 1, "只新增本轮问答任务，不创建费用或规划");
      assert.equal(created[0].intent, "other");
      assert.equal(created[0].subject_id, null);
      assert.equal(created[0].status, "completed");
      assert.equal(searches(turn).length, 0);
      assert.equal(turn.events.filter((e) => e.stage === "policy_check").length, 0);
      assert.equal(turn.events.filter((e) => e.stage === "business_result").length, 0);
      const reads = turn.events.filter((e) => e.stage === "read_task_result");
      assert.equal(reads.length, 1, "须读取真实结果而非仅从聊天历史猜测");
      assert.equal(reads[0].result.status, "ok");
      assert.equal(reads[0].result.task_id, expectedTask.id);
      assert.equal(reads[0].result.revision, expectedTask.revision);
      assert.deepEqual(reads[0].result.result, expectedResult);
      assert.match(turn.answer, /未重新查询或核验/);
      assert.doesNotMatch(turn.answer, /全部符合|都符合|均符合|已批准/);
    };
    const first = await send("帮我查南京的酒店，只查住宿，不查交通。这次是中国大陆因公出差，共2天，我是普通员工，1名成人住1间房，没有儿童，没有专项审批。日期还没定。");
    const taskId = first.tasks.find((t) => t.intent === "plan_trip")?.id;
    const sameTask = (turn, prior) => {
      const task = turn.tasks.find((t) => t.id === taskId);
      const old = prior.tasks.find((t) => t.id === taskId);
      assert.ok(task && old, "必须承接原酒店任务");
      assert.equal(task.intent, "plan_trip");
      assert.equal(task.subject_id, old.subject_id);
      assert.equal(task.revision, old.revision + 1);
      assert.equal(turn.tasks.filter((t) => t.intent === "plan_trip").length, 1);
      return task;
    };
    await check("缺酒店日期时追问且不查询供应商", () => {
      healthy(first);
      assert.ok(taskId);
      assert.equal(first.tasks.find((t) => t.id === taskId).status, "needs_clarification");
      assert.equal(searches(first).length, 0);
      assert.match(first.answer, /入住/);
      assert.match(first.answer, /退房|离店/);
    });
    const partial = await send(`入住日期是${dates.checkIn}，退房日期还没确定。`);
    await check("只补入住日期继续追问而不查询", () => {
      healthy(partial);
      assert.equal(sameTask(partial, first).status, "needs_clarification");
      assert.equal(searches(partial).length, 0);
      assert.match(partial.answer, /退房|离店/);
    });
    const ready = await send(`${dates.checkOut}退房，其他条件不变。`);
    const assertSearch = (turn, city, checkIn, checkOut) => {
      healthy(turn);
      const calls = searches(turn);
      assert.equal(calls.length, 1, "应仅执行一次酒店查询，不查询交通");
      assert.equal(calls[0].kind, "hotel");
      assert.equal(calls[0].request.destination, city);
      assert.equal(calls[0].request.check_in, checkIn);
      assert.equal(calls[0].request.check_out, checkOut);
      assert.equal(calls[0].request.query_scope, "hotel");
      assert.equal(calls[0].request.hotel_rooms, 1);
      assert.equal(calls[0].request.hotel_adults, 1);
      assert.deepEqual(calls[0].request.hotel_child_ages, []);
      assert.equal(calls[0].request.long_assignment_days, 2, "保留明确的短途出差天数");
      assert.equal(calls[0].request.room_occupants, 1, "保留明确的每间房入住人数");
      assert.equal(calls[0].result?.status, "ok", "供应商须真实返回候选才算查询通过");
      assert.ok(calls[0].result.candidates.length > 0);
      const details = calls[0].result.candidates.map((c) => c.details.hotel_detail)
        .filter((d) => d?.status === "ok");
      assert.ok(details.length > 0, "须取得真实房型详情");
      for (const detail of details) {
        assert.equal(detail.requested.checkIn, checkIn);
        assert.equal(detail.requested.checkOut, checkOut);
        assert.equal(detail.requested.roomNum, 1);
        assert.equal(detail.requested.adultNum, 1);
        assert.equal(detail.requested.childNum, 0);
        assert.deepEqual(detail.requested.childAges, []);
        assert.equal(detail.date_verified, false);
        assert.equal(detail.inventory_verified, false);
        assert.ok(detail.rates.length > 0);
        assert.ok(detail.rates.every((r) => r.basis === "unknown" && r.taxes_confirmed === false));
      }
      assert.ok(plan(turn)?.selected.length > 0, "应选出真实候选");
      const { subject_id, revision, fact_context, ...businessResult } = savedPlan(turn);
      assert.deepEqual(businessResult, plan(turn), "保存前不得改写规划器业务结果");
      const task = turn.tasks.find((t) => t.id === taskId);
      assert.equal(subject_id, task.subject_id);
      assert.equal(revision, task.revision);
      assert.equal(fact_context.subject_id, subject_id);
      assert.equal(fact_context.revision, revision);
      for (const item of plan(turn).selected) {
        assert.ok(calls[0].result.candidates.some((c) => c.id === item.id));
        assert.ok(turn.answer.includes(item.label), "展示的候选须来自实际查询");
      }
    };
    await check("补齐日期后查询南京真实酒店并保持任务", () => {
      sameTask(ready, partial);
      assertSearch(ready, "南京", dates.checkIn, dates.checkOut);
    });
    const city = await send("地点改成杭州，其他不变，重新查一下酒店。");
    await check("修改城市重新查询且沿用住宿日期", () => {
      sameTask(city, ready);
      assertSearch(city, "杭州", dates.checkIn, dates.checkOut);
    });
    const changed = await send(`入住改成${dates.changedIn}，${dates.changedOut}退房，重新查一下，其他条件不变。`);
    await check("修改住宿日期后不复用旧候选结果", () => {
      sameTask(changed, city);
      assertSearch(changed, "杭州", dates.changedIn, dates.changedOut);
      const previousIds = new Set((plan(city)?.selected || []).map((c) => c.id));
      assert.ok(plan(changed).selected.every((c) => !previousIds.has(c.id)));
    });
    await check("酒店起价不被判定为已确认合规", () => {
      const draft = plan(changed);
      assert.ok(draft?.policy_checks.length > 0);
      assert.ok(draft.policy_checks.every((c) =>
        c.status === "insufficient_information" && c.approved !== true));
      assert.match(changed.answer, /待.*核|未.*核|尚未.*核|不能确认|信息不足/);
    });
    const question = await send("那这些酒店都符合公司报销标准吗？");
    await check("追问酒店合规时不把供应商起价当作实际费用", () => {
      healthy(question);
      assert.equal(searches(question).length, 0);
      assert.deepEqual(question.tasks.find((t) => t.id === taskId),
        changed.tasks.find((t) => t.id === taskId), "只读追问不修改原任务");
      assert.equal(question.tasks.filter((t) => t.intent === "policy_check").length, 0,
        "不得把候选追问新建成实际费用核验");
      const reads = question.events.filter((e) => e.stage === "read_task_result");
      assert.equal(reads.length, 1, "须读取真实结果而非仅从聊天历史猜测");
      assert.equal(reads[0].result.status, "ok");
      assert.equal(reads[0].result.task_id, taskId);
      assert.deepEqual(reads[0].result.result, savedPlan(changed));
      assert.match(question.answer, /未重新查询或核验/);
      assert.match(question.answer, /无法|不能|不足|未确认|待确认|补充|核实/);
      assert.doesNotMatch(question.answer, /全部符合|都符合|均符合|已批准/);
      for (const event of question.events.filter((e) => e.stage === "policy_check")) {
        assert.notEqual(event.result.status, "within_checked_rules");
        assert.notEqual(event.result.approved, true);
      }
    });
    const expense = await send(`另外核验一笔已经发生的住宿费用：${dates.expense}在杭州，普通员工，中国大陆因公出差共2天，没有专项审批，1人单独住一间，实际支付人民币400元。这是该晚每间房的含税价格，入住日期与账单已核实。`);
    await check("明确实际费用后进入规则核验并引用政策而非宣布批准", () => {
      healthy(expense);
      assert.equal(searches(expense).length, 0);
      const checks = expense.events.filter((e) => e.stage === "policy_check");
      assert.equal(checks.length, 1);
      assert.equal(checks[0].facts.expense_date, dates.expense);
      assert.equal(checks[0].facts.destination, "杭州");
      assert.equal(checks[0].facts.amount, "400");
      assert.equal(checks[0].result.status, "within_checked_rules");
      assert.equal(checks[0].result.approved, false);
      assert.ok(checks[0].result.sources.length > 0);
      assert.match(expense.answer, /不是.*批准|不.*代表.*批准|不等于.*批准/);
    });
    const expenseTask = expense.tasks.find((t) => t.intent === "policy_check");
    const expenseResult = expense.events.findLast((e) =>
      e.stage === "business_result" && e.tool === "consult_policy")?.result;
    const expenseFollowup = await send("刚才那笔已经支付的住宿费，核验结论是什么？我只是查看刚才的结果，不用重新核验。");
    await check("多任务中明确查看实际费用结果不误读酒店方案", () => {
      assertRead(expenseFollowup, expense, expenseTask, expenseResult);
      assert.match(expenseFollowup.answer, /不是.*批准|不.*代表.*批准|不等于.*批准/);
    });
    const hotelFollowup = await send("再看前面杭州的酒店候选方案，现在还缺哪些信息才能判断报销合规？只看保存的结果，不要重新查。");
    await check("从实际费用切回酒店结果不新建费用或执行查询", () => {
      assertRead(hotelFollowup, expenseFollowup, changed.tasks.find((t) => t.id === taskId),
        savedPlan(changed));
      assert.match(hotelFollowup.answer, /信息不足|核实/);
    });
    const variation = await send("把前面待预订的酒店方案目的地改为苏州，入住退房日期和其他条件不变，请重新搜索。已经发生的那笔住宿费不要修改。");
    await check("多任务中修改酒店城市不影响独立实际费用", () => {
      sameTask(variation, hotelFollowup);
      assertSearch(variation, "苏州", dates.changedIn, dates.changedOut);
      assert.deepEqual(variation.tasks.find((t) => t.id === expenseTask.id), expenseTask);
    });
    const variationFollowup = await send("上面推荐的苏州住宿现在能直接按公司规定报销吗？请依据刚才的查询结果说明，不再搜索，也没有新增费用。");
    await check("换城市及措辞的候选追问保持只读和合规边界", () => {
      assertRead(variationFollowup, variation, variation.tasks.find((t) => t.id === taskId),
        savedPlan(variation));
      assert.match(variationFollowup.answer, /信息不足|不能确认|核实/);
    });
    await page.reload();
    await expect(page.locator("#chat-input")).toBeEnabled();
    await check("刷新恢复最后回答和任务状态", async () => {
      assert.equal(await page.locator("#chat-messages .assistant").last().innerText(), variationFollowup.answer);
    });
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1000 });
      await page.screenshot({ path: path.join(output, `hotel-flow-${width}.png`), fullPage: true });
    }
    await check("浏览器无脚本错误", () => assert.deepEqual(report.errors, []));
    report.stage = "finished";
  } catch (error) {
    report.failed.push({ name: report.stage || "setup", reason: error.message });
    if (page) await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
  } finally {
    save();
    await browser.close();
  }
  console.log(JSON.stringify({ passed: report.passed.length, failed: report.failed.length }));
  if (report.failed.length) process.exitCode = 1;
})().catch((error) => {
  console.error("Hotel flow verification failed:", error.name);
  process.exitCode = 1;
});
