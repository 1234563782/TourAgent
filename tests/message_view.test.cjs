"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { parseChatText } = require("../business_travel/web_static/message-view.js");

test("plans have headings, table rows and closed-by-default detail data", () => {
  const blocks = parseChatText("### Trip\n\n| Leg | Price |\n| --- | --- |\n| Out | 120 |\n\n" +
    ":::details Conditions\nSupplier defaults\nCancellation terms\n:::\n\nImportant warning");
  assert.deepEqual(blocks.map((block) => block.type), ["heading", "table", "details", "text"]);
  assert.deepEqual(blocks[1].rows, [["Out", "120"]]);
  assert.equal(blocks[2].text, "Supplier defaults\nCancellation terms");
  assert.equal(blocks[3].text, "Important warning");
});

test("HTML, URLs and event handlers are text, never executable markup", () => {
  const attack = '<img src=x onerror="alert(1)"><script>bad()</script>[x](javascript:bad())';
  assert.deepEqual(parseChatText(attack), [{ type: "text", text: attack }]);
  const blocks = parseChatText(":::details " + attack + "\n" + attack + "\n:::");
  assert.equal(blocks[0].title, attack);
  assert.equal(blocks[0].text, attack);
});

test("streamed detail contents never spill into the main answer", () => {
  const text = "### Plan\n\n:::details Details\nInternal content\n:::\n\nFinal warning";
  const start = text.indexOf("Internal content");
  for (let size = start + 1; size <= text.length; size++) {
    const blocks = parseChatText(text.slice(0, size), true);
    assert.ok(blocks.some((block) => block.type === "details"));
    assert.ok(!blocks.filter((block) => block.type === "text")
      .some((block) => block.text.includes("Internal")));
  }
  assert.deepEqual(parseChatText(text, true), parseChatText(text));
});

test("plain old conversations and malformed tables stay readable", () => {
  assert.deepEqual(parseChatText("Hello\nworld\n\nSecond paragraph"),
    [{ type: "text", text: "Hello\nworld" }, { type: "text", text: "Second paragraph" }]);
  assert.equal(parseChatText("| one | two |\n| invalid | row |")[0].type, "text");
  assert.equal(parseChatText("你好\n\n:::det", true).length, 1);
  assert.equal(parseChatText(":::details Pending\nbody", true)[0].text, "body");
});
