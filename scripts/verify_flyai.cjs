// Run only public read-only searches; never print credentials or booking URLs.
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { parseArgs } = require("node:util");

const { values } = parseArgs({
  options: {
    cli: { type: "string" },
    kind: { type: "string" },
    origin: { type: "string" },
    destination: { type: "string" },
    date: { type: "string" },
    checkout: { type: "string" },
    output: { type: "string" },
    timeout: { type: "string", default: "45000" },
  },
});

function requireValue(name) {
  if (!values[name]?.trim()) throw new Error(`Missing --${name}`);
  return values[name];
}

function requireDate(name) {
  const value = requireValue(name);
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(parsed.getTime())
      || parsed.toISOString().slice(0, 10) !== value) {
    throw new Error(`Invalid --${name}: expected a real YYYY-MM-DD date`);
  }
  return value;
}

const kind = requireValue("kind");
if (!["flight", "train", "hotel"].includes(kind)) throw new Error("Unsupported search kind");
const cli = path.resolve(requireValue("cli"));
if (!fs.statSync(cli).isFile()) throw new Error("CLI must be a file");
const timeout = Number(values.timeout);
if (!Number.isInteger(timeout) || timeout < 1000 || timeout > 120000) {
  throw new Error("--timeout must be between 1000 and 120000 milliseconds");
}
const date = requireDate("date");
const destination = requireValue("destination");
const args = [cli, `search-${kind}`];
if (kind === "hotel") {
  const checkout = requireDate("checkout");
  if (checkout <= date) throw new Error("Checkout must be after check-in");
  args.push("--dest-name", destination, "--check-in-date", date, "--check-out-date", checkout);
} else {
  args.push("--origin", requireValue("origin"), "--destination", destination, "--dep-date", date);
}

const started = Date.now();
const result = spawnSync(process.execPath, args, {
  encoding: "utf8",
  timeout,
  maxBuffer: 4 * 1024 * 1024,
  windowsHide: true,
  shell: false,
});
let payload;
let parsedJson = false;
try {
  payload = JSON.parse(result.stdout || "");
  parsedJson = true;
} catch {
  // Non-JSON output is a failed verification, even if the process exits zero.
}
const arrays = [];
const priceFields = new Set();
const linkFields = new Set();
const scalarFields = new Set();
function inspect(value, location = "$", depth = 0) {
  if (depth > 12 || value === null || typeof value !== "object") return;
  if (Array.isArray(value)) {
    arrays.push({ path: location, count: value.length });
    value.slice(0, 3).forEach(item => inspect(item, `${location}[]`, depth + 1));
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    if (/price|amount|fare/i.test(key)) priceFields.add(`${location}.${key}`);
    if (/url|link/i.test(key)) linkFields.add(`${location}.${key}`);
    if (child === null || typeof child !== "object") scalarFields.add(`${location}.${key}`);
    inspect(child, `${location}.${key}`, depth + 1);
  }
}
inspect(payload);
const combined = `${result.stdout || ""}\n${result.stderr || ""}`;
const authFailure = /unauthoriz|forbidden|invalid.{0,20}(key|token)|auth.{0,20}fail|鉴权|无权限|未授权|登录|认证失败|HTTP\s*(401|403)/i.test(combined);
const providerFailure = payload?.isError === true || Boolean(payload?.error) || payload?.success === false
  || (payload?.status !== undefined && payload.status !== 0);
const argumentEcho = payload?.tool && payload?.arguments;
const status = authFailure ? "authentication_required"
  : result.error?.code === "ETIMEDOUT" ? "timeout"
  : result.status !== 0 || !parsedJson || providerFailure || argumentEcho ? "failed"
  : "response_received";
const report = {
  checkedAt: new Date().toISOString(),
  kind, date, destination,
  status,
  elapsedMs: Date.now() - started,
  exitCode: result.status,
  nodeVersion: process.version,
  childPid: result.pid,
  assertion: /Assertion failed:[^\r\n]*/.exec(result.stderr || "")?.[0] || null,
  processError: result.error?.code || null,
  parsedJson,
  rootType: Array.isArray(payload) ? "array" : typeof payload,
  rootKeys: payload && typeof payload === "object" ? Object.keys(payload).slice(0, 40) : [],
  providerStatus: typeof payload?.status === "string" || typeof payload?.status === "number" ? payload.status : null,
  trialMode: /体验模式/.test(payload?.systemMessage || ""),
  itemSummary: Array.isArray(payload?.data?.itemList) ? payload.data.itemList.slice(0, 3).map(item => ({
    keys: Object.keys(item),
    ticketPrice: /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(String(item.ticketPrice)) ? String(item.ticketPrice) : null,
    ticketPriceType: typeof item.ticketPrice,
    hasJumpUrl: typeof item.jumpUrl === "string" && item.jumpUrl.startsWith("https://"),
    departure: item.journeys?.[0]?.segments?.[0]?.depDateTime ?? null,
    arrival: item.journeys?.[0]?.segments?.[0]?.arrDateTime ?? null,
    priceValues: Object.fromEntries(Object.entries(item)
      .filter(([key, value]) => /price|amount|fare/i.test(key)
        && /^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(String(value)))),
  })) : [],
  arrays,
  priceFields: [...priceFields],
  linkFields: [...linkFields],
  scalarFields: [...scalarFields].slice(0, 100),
  note: "Response receipt and field names alone do not prove valid inventory or complete prices.",
};
if (values.output) fs.writeFileSync(path.resolve(values.output), JSON.stringify(report, null, 2) + "\n");
console.log(JSON.stringify(report, null, 2));
process.exitCode = status === "response_received" ? 0 : 1;
