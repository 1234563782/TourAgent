// Real university policy workflow, isolated account, no substituted model answers.
const { chromium, expect: baseExpect } = require("playwright/test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const expect = baseExpect.configure({ timeout: 180000 });

(async () => {
  const output = process.env.AUTH_TEST_OUTPUT;
  const report = { passed: [], failed: [], turns: [], errors: [] };
  const save = () => fs.writeFileSync(path.join(output, "summary.json"), JSON.stringify(report, null, 2));
  const browser = await chromium.launch({ executablePath: process.env.AUTH_TEST_BROWSER, headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.setDefaultTimeout(180000);
    page.on("pageerror", e => report.errors.push(e.name));
    await page.goto(process.env.AUTH_TEST_ORIGIN);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.locator("#login-submit").click();
    await expect(page.locator("#workspace")).toBeVisible();
    const check = async (name, run) => {
      try { await run(); report.passed.push(name); }
      catch (e) { report.failed.push({ name, reason: e.message }); }
      save();
    };
    let explanationCount = 0;
    const send = async (question, fresh = true) => {
      if (fresh) {
        await page.locator("#chat-new").click();
        await expect(page.locator("#chat-messages .user")).toHaveCount(0);
      }
      await expect(page.locator("#chat-input")).toBeEnabled();
      console.log("TURN", report.turns.length + 1);
      await page.locator("#chat-input").fill(question);
      const pending = page.waitForResponse(r => r.url().endsWith("/chat/stream") &&
        r.request().method() === "POST");
      await page.locator("#chat-submit").click();
      const response = await pending;
      assert.equal(response.status(), 200);
      await expect(page.locator("#chat-input")).toBeEnabled();
      await expect(page.locator("#chat-error")).toBeHidden();
      const history = await context.request.get(response.url().replace(/\/stream$/, ""));
      assert.equal(history.status(), 200);
      const state = await history.json();
      const answer = await page.locator("#chat-messages .assistant").last().innerText();
      const diagnostics = path.join(output, "policy-explanations.json");
      const explanations = fs.existsSync(diagnostics) ? JSON.parse(fs.readFileSync(diagnostics, "utf8")) : [];
      const turnExplanations = explanations.slice(explanationCount);
      const explanation = turnExplanations.at(-1) ?? null;
      explanationCount = explanations.length;
      const turn = { question, answer, tasks: state.tasks, explanations: turnExplanations,
        policy_request: explanation?.test_request ?? null,
        lodging_lookup: explanation?.test_lookup ?? null,
        requirements: explanation?.test_requirements ?? null,
        proposal: explanation?.rag?.proposal ?? null };
      report.turns.push(turn);
      save();
      return turn;
    };
    const cited = answer => {
      assert.match(answer, /\[S\d+\]/);
      assert.match(answer, /2026-project-v1/);
      assert.doesNotMatch(answer, /信息提取未通过|暂不可用|依据不完整|证据不足/);
    };
    const applicableCap = (answer, amount) => {
      // Exclude source footnotes: mentioning an amount in a citation is not an applicable conclusion.
      const claims = answer.split(/(?:\r?\n)/).filter(line => !/^\s*\[S\d+\]/.test(line)).join("\n");
      assert.match(claims, new RegExp(`(?:适用|上限|限额|标准|执行|按)[^。\\n]{0,90}${amount}`));
    };
    const personal = turn => {
      assert.deepEqual(turn.policy_request?.policy_query, { category: "hotel", mode: "personal_limit" });
      assert.equal(turn.policy_request?.facts.kind, "hotel");
      assert.ok(turn.lodging_lookup);
    };
    const pureClarification = (turn, fields) => {
      personal(turn);
      assert.deepEqual([...turn.requirements.missing_fields].sort(), [...fields].sort());
      assert.deepEqual([...turn.proposal.requested_fields].sort(), [...fields].sort());
      assert.doesNotMatch(turn.answer, /\d+(?:\.\d+)?\s*元|旺季|淡季|平季|\d+\s*[-—至到]\s*\d+\s*月/);
    };
    const units = await send("请按2026年的学校办法解释：住宿附表是元/人·天还是元/间夜？限额是否就是固定发给我的金额？只讲制度，不核验费用。");
    await check("学校住宿单位与实报条件", () => {
      cited(units.answer);
      assert.match(units.answer, /人.*天/);
      assert.match(units.answer, /据实|实际|凭据|实报/);
      assert.doesNotMatch(units.answer, /政策适用日期：|请.*(?:生效|入住日期)/);
      assert.ok(units.policy_request);
      assert.equal(units.policy_request.facts.expense_date ?? null, null);
    });
    const nonseasonal = await send("按学校现行办法，江苏省南京市的住宿上限是多少？住宿按附表其他人员档，只解释个人适用标准，出差日期尚未确定。");
    await check("无季节差价不追问入住日期", () => {
      cited(nonseasonal.answer);
      applicableCap(nonseasonal.answer, 380);
      assert.equal(nonseasonal.tasks.find(t => t.intent === "policy_explain")?.status, "completed");
      assert.equal(nonseasonal.policy_request?.facts.expense_date ?? null, null);
      personal(nonseasonal);
      assert.equal(nonseasonal.lodging_lookup.status, "ok");
    });
    const missing = await send("按2026年学校差旅制度，我住宿上限是多少？我还没有说目的地和适用住宿档次，只解释标准。");
    const task = missing.tasks.find(t => t.intent === "policy_explain");
    await check("缺城市和住宿档次主动追问", () => {
      assert.equal(task?.status, "needs_clarification");
      assert.match(missing.answer, /目的地|城市|地区/);
      assert.match(missing.answer, /档次|级别|人员类别|人员类型/);
      assert.doesNotMatch(missing.answer, /普通员工|部门经理/);
      assert.ok(missing.policy_request);
      assert.equal(missing.policy_request.facts.expense_date ?? null, null);
      pureClarification(missing, ["destination", "lodging_standard"]);
    });
    const city = await send("目的地是山东省青岛市。", false);
    await check("只补城市不猜住宿档次", () => {
      assert.equal(city.tasks.find(t => t.id === task?.id)?.status, "needs_clarification");
      assert.match(city.answer, /档次|级别|人员类别|人员类型/);
      assert.doesNotMatch(city.answer, /\d+(?:\.\d+)?\s*元/);
      pureClarification(city, ["lodging_standard"]);
      assert.equal(city.policy_request.request_context.original_request, missing.question);
    });
    const undated = await send("住宿按附表的其他人员档，还没确定入住日期。", false);
    await check("青岛有季节差价仅追问尚缺入住日期", () => {
      assert.equal(undated.tasks.find(t => t.id === task?.id)?.status, "needs_clarification");
      assert.match(undated.answer, /日期|几月|什么时候|哪天/);
      assert.doesNotMatch(undated.answer, /\d+(?:\.\d+)?\s*元|政策.*生效|适用.*年份/);
      pureClarification(undated, ["expense_date"]);
    });
    const rank = await send("入住日期是2026-09-26。", false);
    await check("同事项补齐后引用旺季450", () => {
      cited(rank.answer);
      assert.match(rank.answer, /450/);
      applicableCap(rank.answer, 450);
      assert.match(rank.answer, /Sheet1/);
      assert.equal(rank.tasks.find(t => t.id === task?.id)?.status, "completed");
      assert.equal(rank.policy_request?.facts.expense_date, "2026-09-26");
      personal(rank);
      assert.equal(Number(rank.lodging_lookup.cap), 450);
      assert.ok(rank.lodging_lookup.row_ids.length);
    });
    const changed = await send("把刚才的入住日期改为2026-10-01，青岛和其他人员档不变，重新解释住宿上限。", false);
    await check("日期更正切到平季380而非450", () => {
      cited(changed.answer);
      assert.match(changed.answer, /380/);
      applicableCap(changed.answer, 380);
      const current = changed.tasks.find(t => t.id === task?.id);
      assert.equal(current?.status, "completed");
      assert.equal(current.revision, rank.tasks.find(t => t.id === task.id).revision + 1);
      assert.equal(changed.policy_request?.facts.expense_date, "2026-10-01");
      personal(changed);
      assert.equal(Number(changed.lodging_lookup.cap), 380);
    });
    const withdrawn = await send("撤回刚才的入住日期，现在还不确定哪天出差。其他条件不变，先只介绍学校住宿费用的计价单位，不用给具体金额。", false);
    await check("撤回业务日期仍可解释一般政策", () => {
      cited(withdrawn.answer);
      assert.match(withdrawn.answer, /人.*天/);
      assert.doesNotMatch(withdrawn.answer, /哪一天适用的政策|请.*日期|政策适用日期：/);
      assert.ok(withdrawn.policy_request);
      assert.equal(withdrawn.policy_request.facts.expense_date ?? null, null);
      assert.equal(withdrawn.policy_request.policy_query.mode, "general");
    });
    const train = await send("学校差旅办法里，专业技术五级岗位人员国内因公出差可以坐高铁一等座吗？只解释标准。");
    await check("学校岗位交通标准有依据", () => {
      cited(train.answer);
      assert.match(train.answer, /一等座/);
      assert.match(train.answer, /二类|第二类/);
    });
    const insurance = await send("按学校差旅制度，买了交通意外保险是不是就代表出差时任何意外都能赔？我没有提供保单，只解释制度的边界。");
    await check("保险购买报销不等同保险理赔", () => {
      assert.doesNotMatch(insurance.answer, /信息提取未通过|暂不可用/);
      assert.match(insurance.answer, /保单|保险合同|保险条款|保障范围|赔付范围/);
      assert.match(insurance.answer, /不能|不等|不足|不代表|无法/);
    });
    const independent = await send("我有两趟独立的出差，分别去四川省成都市和福建省厦门市，入住时间都还没定。两趟住宿都按附表其他人员档。请分别解释我的住宿上限，不查酒店，也不核验已发生的费用。");
    const findExplanation = (turn, city) => turn.explanations.find(item =>
      item.test_request?.facts.destination?.includes(city));
    const chengdu = findExplanation(independent, "成都");
    const xiamen = findExplanation(independent, "厦门");
    await check("不同城市同轮建立独立政策事项", () => {
      cited(independent.answer);
      assert.equal(independent.tasks.filter(t => t.intent === "policy_explain").length, 2);
      assert.equal(independent.explanations.length, 2);
      assert.equal(Number(chengdu?.test_lookup?.cap), 370);
      assert.equal(Number(xiamen?.test_lookup?.cap), 400);
      for (const item of [chengdu, xiamen]) {
        assert.equal(item.test_request.facts.expense_date ?? null, null);
        assert.equal(item.rag.status, "answered");
        assert.equal(item.test_request.policy_query.mode, "personal_limit");
      }
      assert.notEqual(chengdu.test_request.request_context.task_id, xiamen.test_request.request_context.task_id);
      applicableCap(independent.answer, 370);
      applicableCap(independent.answer, 400);
    });
    const chengduId = chengdu?.test_request?.request_context?.task_id;
    const xiamenId = xiamen?.test_request?.request_context?.task_id;
    const changedGrade = await send("只更正成都这趟：住宿人员档次改成附表司局级，重新解释上限。厦门那趟保持原样。", false);
    await check("指定事项改档次不改另一个事项", () => {
      assert.ok(chengduId && xiamenId);
      cited(changedGrade.answer);
      applicableCap(changedGrade.answer, 470);
      assert.equal(changedGrade.explanations.length, 1);
      assert.equal(Number(findExplanation(changedGrade, "成都")?.test_lookup?.cap), 470);
      assert.equal(changedGrade.tasks.find(t => t.id === chengduId)?.revision,
        independent.tasks.find(t => t.id === chengduId).revision + 1);
      assert.deepEqual(changedGrade.tasks.find(t => t.id === xiamenId),
        independent.tasks.find(t => t.id === xiamenId));
    });
    const withdrawnGrade = await send("成都的住宿档次我刚才说错了，先撤回，目前不能确定属于哪档，仍然是问我这趟的上限。厦门那趟不要动。", false);
    await check("指定事项撤回档次仅追问该字段", () => {
      assert.ok(chengduId && xiamenId);
      assert.equal(withdrawnGrade.explanations.length, 1);
      pureClarification(withdrawnGrade, ["lodging_standard"]);
      assert.equal(withdrawnGrade.tasks.find(t => t.id === chengduId)?.status, "needs_clarification");
      assert.deepEqual(withdrawnGrade.tasks.find(t => t.id === xiamenId),
        independent.tasks.find(t => t.id === xiamenId));
    });
    const planning = await send("帮我查2026-10-13入住、10月14日退房的南京酒店，1名成人1间房每房1人，没有儿童，预算每晚400元。此次是高校教职工大陆因公出差，住宿按其他人员档，江苏省。只给参考候选，不下单。");
    await check("高校酒店规划保留真实报价不确定性", () => {
      assert.doesNotMatch(planning.answer, /信息提取未通过|暂不可用/);
      assert.match(planning.answer, /候选|房型/);
      assert.match(planning.answer, /报价|价格/);
      assert.match(planning.answer, /未.*核实|待核实|未确认|尚未|不能.*合规/);
      assert.doesNotMatch(planning.answer, /请补充或核实：.*职级/);
      assert.doesNotMatch(planning.answer, /请补充或核实：.*住宿.*档次/);
      assert.equal(planning.tasks.filter(t => t.intent === "plan_trip").length, 1);
    });
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 1000 });
      await check(`${width}宽度输入框可见且无页面横溢出`, async () => {
        await expect(page.locator("#chat-input")).toBeVisible();
        await expect(page.locator("#chat-submit")).toBeVisible();
        assert.ok(await page.evaluate(() =>
          document.documentElement.scrollWidth <= window.innerWidth));
      });
      await page.screenshot({ path: path.join(output, `university-${width}.png`), fullPage: true });
    }
    await page.reload();
    await expect(page.locator("#workspace")).toBeVisible();
    await check("刷新保留最后一轮回答", async () => {
      assert.equal(await page.locator("#chat-messages .assistant").last().innerText(), planning.answer);
    });
  } catch (e) {
    report.errors.push({ type: e.name, reason: e.message });
  } finally {
    save();
    await browser.close();
  }
  console.log(JSON.stringify({ passed: report.passed.length, failed: report.failed.length,
    errors: report.errors.length }));
  process.exitCode = report.failed.length || report.errors.length ? 1 : 0;
})();
