// Real policy renderer and message component; all browser resources are local fixtures.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const output = path.join(root, ".verification", "policy-citations");
const origin = "http://127.0.0.1:48174";
const text = execFileSync(path.join(root, ".venv", "Scripts", "python.exe"), ["-c",
  "from tests.test_rag_corrections import presentation_fixture; from business_travel.policy_guard import render_policy; print(render_policy(presentation_fixture()))"],
{ cwd: root, encoding: "utf8", env: { ...process.env, PYTHONPATH: ".;tests", PYTHONIOENCODING: "utf-8" } }).trim();

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
      if (url.pathname === "/") return route.fulfill({ contentType: "text/html", body:
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<link rel="stylesheet" href="/app.css"><main class="workspace"><h1>政策查询</h1>' +
        '<article id="first" class="chat-message assistant"></article>' +
        '<article id="second" class="chat-message assistant"></article></main><script src="/message-view.js"></script></html>',
      });
      const name = url.pathname.slice(1);
      assert.ok(["app.css", "message-view.js"].includes(name));
      return route.fulfill({ path: path.join(root, "business_travel", "web_static", name),
        contentType: name.endsWith(".js") ? "text/javascript" : "text/css" });
    });
    await page.goto(origin);
    await page.evaluate((text) => {
      renderChatText(document.getElementById("first"), text);
      renderChatText(document.getElementById("second"), text);
    }, text);
    const first = page.locator("#first");
    const second = page.locator("#second");
    assert.equal(await first.locator(".citation-reference").count(), 2);
    assert.equal(await page.locator("details[open]").count(), 0);
    await first.locator(".citation-reference").nth(1).click();
    assert.equal(await first.locator("details[open] summary").innerText(), "政策依据 · 来源2");
    assert.match(await first.locator("details[open]").innerText(), /Sheet1 \/ B9:K9/);
    assert.match(await first.locator("details[open]").innerText(), /部级：1100/);
    assert.equal(await second.locator("details[open]").count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement.textContent), "政策依据 · 来源2");
    const stable = await page.evaluate((text) => {
      const container = document.getElementById("first");
      renderChatText(container, text + "\n", { streaming: true });
      return container.querySelectorAll("details[open]").length === 1 &&
        document.activeElement.textContent === "政策依据 · 来源2";
    }, text);
    assert.equal(stable, true);
    await second.locator(".citation-reference").first().focus();
    await page.keyboard.press("Enter");
    assert.equal(await second.locator("details[open] summary").innerText(), "政策依据 · 来源1");
    const security = await page.evaluate(() => {
      const container = document.createElement("div");
      document.body.append(container);
      const attack = '<img src=x onerror="window.PWNED=true"><script>window.PWNED=true</script>';
      renderChatText(container, `[来源3]\n${attack}\n:::details 政策依据 · 来源1\n${attack}\n:::`);
      const safe = container.querySelectorAll("script,img").length === 0 && !window.PWNED;
      const missingIsPlain = container.querySelectorAll(".citation-reference").length === 0 &&
        container.textContent.includes("[来源3]");
      container.remove();
      return { safe, missingIsPlain };
    });
    assert.deepEqual(security, { safe: true, missingIsPlain: true });
    const combinedTargets = await page.evaluate((text) => {
      const container = document.createElement("div");
      document.body.append(container);
      renderChatText(container, text + "\n\n" + text);
      container.querySelectorAll(".citation-reference")[0].click();
      const firstMatches = container.querySelectorAll("details")[0].open &&
        !container.querySelectorAll("details")[2].open;
      container.querySelectorAll(".citation-reference")[3].click();
      const secondMatches = container.querySelectorAll("details")[3].open &&
        !container.querySelectorAll("details")[1].open;
      container.remove();
      return { firstMatches, secondMatches };
    }, text);
    assert.deepEqual(combinedTargets, { firstMatches: true, secondMatches: true });
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 1100 });
      await page.evaluate(() => document.querySelectorAll("details").forEach((node) => { node.open = false; }));
      await first.scrollIntoViewIfNeeded();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      for (const reference of await page.locator(".citation-reference").all()) {
        const box = await reference.boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width);
      }
      await page.screenshot({ path: path.join(output, `policy-${width}.png`), fullPage: true });
      await first.locator(".citation-reference").nth(1).click();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `policy-evidence-${width}.png`), fullPage: true });
    }
    assert.deepEqual(errors, []);
    const report = { passed: true, mode: "isolated-fixtures",
      checks: ["real-python-renderer", "collapsed-by-default", "click-matching-evidence", "same-message-scope",
        "keyboard-activation", "stream-preserves-open-and-focus", "inert-html", "missing-reference-plain-text",
        "combined-tool-results-same-number-isolation",
        "desktop-1440", "mobile-390", "mobile-320"],
      boundary: "No real model, supplier, database or running service was used." };
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
