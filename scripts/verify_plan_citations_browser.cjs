// Exercise itinerary citations through the real app, using local API fixtures.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const output = path.join(root, ".verification", "plan-citations");
const origin = "http://127.0.0.1:48175";
const text = execFileSync(path.join(root, ".venv", "Scripts", "python.exe"), ["-c",
  "from tests.test_plan_policy_citations import recommendation_fixture; from business_travel.reply_renderer import render_tool_result; print(render_tool_result('plan_trip', {'draft': recommendation_fixture()[0]}))"],
{ cwd: root, encoding: "utf8", env: { ...process.env, PYTHONPATH: ".;tests", PYTHONIOENCODING: "utf-8" } }).trim();
const conversation = { id: "plan-citation-fixture", title: "南京北京差旅（验收数据）", updated_at: "2026-10-18T10:00:00Z" };
const state = { conversation_id: conversation.id, messages: [{ role: "assistant", text }],
  revision: "fixture-1", interrupt: [], run: null };

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true,
    ...(process.env.AUTH_TEST_BROWSER ? { executablePath: process.env.AUTH_TEST_BROWSER } : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.origin, origin);
      const pathname = url.pathname;
      const json = (data) => route.fulfill({ json: data });
      if (pathname === "/") return route.fulfill({ contentType: "text/html",
        path: path.join(root, "business_travel", "web_static", "index.html") });
      if (pathname.startsWith("/static/")) {
        const name = path.basename(pathname);
        assert.ok(["app.css", "app.js", "message-view.js", "chat-stream.js", "brand.svg"].includes(name));
        return route.fulfill({ path: path.join(root, "business_travel", "web_static", name),
          contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "image/svg+xml" });
      }
      if (pathname === "/api/auth/me") return json({ username: "引用验收", company_id: "fixture", user_id: "fixture" });
      if (pathname === "/api/memory") return json({ trip_history: [] });
      if (pathname === "/api/conversations") return json({ items: [conversation], next_offset: null });
      if (pathname.endsWith("/runs")) return json({ run: null });
      if (pathname.endsWith("/chat")) return json(state);
      throw new Error("Unexpected fixture request: " + pathname);
    });
    await page.goto(origin);
    const answer = page.locator(".chat-message.assistant").last();
    await answer.locator(".citation-reference").first().waitFor();
    assert.equal(await answer.locator("details[open]").count(), 0);
    assert.match(await answer.innerText(), /住宿参考上限：1100元\/人·天 \[来源2\]/);
    assert.equal(await answer.locator("details summary").filter({ hasText: /^政策依据 · 来源\d+$/ }).count(), 2);
    const rendered = await answer.textContent();
    assert.ok(!rendered.includes('"title_categories"'));
    assert.ok(!rendered.includes("标准：二类；对应人员："));
    assert.ok(!rendered.includes("第三十五条"));
    assert.equal(rendered.split("标准：一类；对应人员：").length - 1, 1);
    assert.ok(!rendered.includes("项目推荐口径 /"));
    assert.ok(!rendered.includes("非制度原文"));
    assert.ok(!rendered.includes("项目兼容默认值"));
    assert.match(rendered, /本次住宿按部级标准提供推荐参考/);
    assert.equal(await answer.getByRole("button", { name: /项目口径/ }).count(), 0);
    await answer.locator(".citation-reference").first().click();
    assert.match(await answer.locator("details[open]").innerText(), /第六条/);
    assert.match(await answer.locator("details[open]").innerText(), /院士/);
    assert.ok(!(await answer.locator("details[open]").innerText()).includes("非制度原文"));
    await answer.locator("details[open] summary").click();
    const originalRefs = answer.getByRole("button", { name: "查看来源1的依据摘录", exact: true });
    await originalRefs.last().click();
    assert.equal(await answer.locator("details[open] summary").innerText(), "政策依据 · 来源1");
    await answer.locator("details[open] summary").click();
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 });
      const reference = answer.getByRole("button", { name: "查看来源2的依据摘录", exact: true });
      await reference.scrollIntoViewIfNeeded();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const box = await reference.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width);
      await page.screenshot({ path: path.join(output, `plan-standards-${width}.png`), fullPage: true });
      await reference.click();
      const evidence = answer.locator("details[open]");
      assert.equal(await evidence.count(), 1);
      assert.match(await evidence.innerText(), /Sheet1 \/ B9:K9/);
      assert.match(await evidence.innerText(), /原文摘录/);
      assert.match(await evidence.innerText(), /1100/);
      assert.equal(await page.evaluate(() => document.activeElement.textContent), "政策依据 · 来源2");
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `plan-evidence-${width}.png`), fullPage: true });
      await evidence.locator("summary").click();
    }
    await page.reload();
    await page.locator(".citation-reference").first().waitFor();
    assert.equal(await page.locator("details[open]").count(), 0);
    assert.deepEqual(errors, []);
    const report = { passed: true, mode: "real-app-with-local-fixtures",
      checks: ["itinerary-renderer", "real-app-message-loading", "standard-field-references", "collapsed-default",
        "deduplicated-fragments", "no-unrelated-categories", "original-sources-only", "no-config-json",
        "lodging-assumption-at-end", "transport-original", "exact-lodging-row", "click-to-expand-and-focus", "reload-references",
        "desktop-1440", "mobile-390", "mobile-320"],
      boundary: "Published policy package with simulated database; no real model, supplier or running service." };
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
