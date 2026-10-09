const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.AUTH_TEST_BROWSER,
    headless: true,
  });
  const output = process.env.AUTH_TEST_OUTPUT;
  const origin = process.env.AUTH_TEST_ORIGIN;
  const errors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.name));
    await page.goto(origin);
    await page.getByRole("heading", { name: "登录工作台" }).waitFor();
    await page.screenshot({ path: path.join(output, "login-desktop.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    const logo = page.locator(".brand img");
    assert.equal(await logo.evaluate((image) => image.complete && image.naturalWidth > 0), true);
    await page.getByLabel("账号", { exact: true }).fill("browser_test");
    await page.getByLabel("密码", { exact: true }).fill("incorrect-password");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "账号或密码错误" }).waitFor();
    assert.equal(await page.getByLabel("密码", { exact: true }).inputValue(), "");
    await page.getByLabel("密码", { exact: true }).fill(process.env.AUTH_TEST_PASSWORD);
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await page.getByRole("heading", { name: "我的差旅" }).waitFor();
    await page.locator("#workspace").waitFor({ state: "visible" });
    await page.screenshot({ path: path.join(output, "workspace-desktop.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.cookie.includes("travel_session")), false);
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    await page.reload();
    await page.locator("#workspace").waitFor({ state: "visible" });
    await page.getByRole("tab", { name: "历史行程" }).click();
    await page.getByText("暂无历史行程", { exact: true }).waitFor();
    await page.getByRole("button", { name: "刷新", exact: true }).click();
    await page.getByText("暂无历史行程", { exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(output, "workspace-mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.getByRole("button", { name: "退出登录" }).click();
    await page.getByRole("heading", { name: "登录工作台" }).waitFor();
    await page.screenshot({ path: path.join(output, "login-mobile.png"), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal((await context.request.get(origin + "/api/memory")).status(), 401);
    await page.setViewportSize({ width: 320, height: 640 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log("PASS: desktop/mobile, assets, failed login, login, HttpOnly, reload, tabs, refresh, logout; no JS errors");
  } finally {
    await browser.close();
  }
})().catch((error) => {
  // Do not print Playwright call logs: form values can be credentials.
  console.error("Browser verification failed:", error.name);
  process.exitCode = 1;
});
