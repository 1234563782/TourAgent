"use strict";

// A deliberately limited presentation format: plain text, headings, tables and
// detail blocks. All content, including HTML and URLs, remains inert text.
function parseChatText(text, streaming = false) {
  const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
  if (streaming && /^:{1,3}(?:d(?:e(?:t(?:a(?:i(?:l(?:s)?)?)?)?)?)?)?(?: .*)?$/.test(lines.at(-1))) {
    lines.pop();
  }
  const blocks = [];
  const cells = (line) => line.trim().slice(1, -1).split("|").map((value) => value.trim());
  const isRow = (line) => /^\|.*\|$/.test(line?.trim() || "");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    if (line.startsWith(":::details ")) {
      const title = line.slice(":::details ".length);
      const content = [];
      index += 1;
      while (index < lines.length && lines[index] !== ":::") content.push(lines[index++]);
      if (lines[index] === ":::") index += 1;
      blocks.push({ type: "details", title, text: content.join("\n") });
      continue;
    }
    const heading = /^(#{2,4}) (.+)$/.exec(line);
    if (heading) {
      blocks.push({ type: "heading", text: heading[2] });
      index += 1;
      continue;
    }
    if (isRow(line) && isRow(lines[index + 1])) {
      const header = cells(line);
      const separator = cells(lines[index + 1]);
      if (header.length === separator.length && separator.every((cell) => /^:?-{3,}:?$/.test(cell))) {
        const rows = [];
        index += 2;
        while (isRow(lines[index]) && cells(lines[index]).length === header.length) {
          rows.push(cells(lines[index++]));
        }
        blocks.push({ type: "table", header, rows });
        continue;
      }
    }
    const previous = blocks.at(-1);
    if (previous?.type === "text" && index > 0 && lines[index - 1].trim()) previous.text += "\n" + line;
    else blocks.push({ type: "text", text: line });
    index += 1;
  }
  return blocks;
}

function renderChatText(container, text, { streaming = false } = {}) {
  const document = container.ownerDocument;
  const opened = new Map(Array.from(container.querySelectorAll("details"), (detail) =>
    [detail.dataset.detailKey, detail.open]));
  const focusKey = container.contains(document.activeElement) &&
    document.activeElement?.parentElement?.dataset.detailKey;
  const fragment = document.createDocumentFragment();
  let detailIndex = 0;
  const blocks = parseChatText(text, streaming);
  const evidencePositions = new Map();
  blocks.forEach((block, index) => {
    if (block.type === "details" && /^政策依据 · 来源[1-9]\d*$/.test(block.title)) {
      if (!evidencePositions.has(block.title)) evidencePositions.set(block.title, []);
      evidencePositions.get(block.title).push(index);
    }
  });
  const evidenceDetails = new Map();
  function element(tag, content) {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    return node;
  }
  function citedParagraph(text, blockIndex) {
    const paragraph = element("p");
    let offset = 0;
    for (const match of text.matchAll(/\[来源([1-9]\d*)\]/g)) {
      const title = `政策依据 · 来源${match[1]}`;
      const targetIndex = evidencePositions.get(title)?.find((index) => index > blockIndex);
      if (targetIndex === undefined) continue;
      const projectRule = blocks[targetIndex].text.startsWith("项目推荐口径 / ");
      paragraph.append(document.createTextNode(text.slice(offset, match.index)));
      const reference = element("button", projectRule ? "[项目口径]" : match[0]);
      reference.type = "button";
      reference.className = "citation-reference";
      reference.title = `查看来源${match[1]}的依据摘录`;
      if (projectRule) reference.title += "（项目口径，非制度原文）";
      reference.setAttribute("aria-label", reference.title);
      reference.addEventListener("click", () => {
        // Several tool results can share one message and restart source numbering.
        const detail = evidenceDetails.get(title)?.find((entry) => entry.index > blockIndex)?.node;
        if (!detail) return;
        detail.open = true;
        detail.querySelector("summary").focus({ preventScroll: true });
        detail.scrollIntoView({ block: "nearest" });
      });
      paragraph.append(reference);
      offset = match.index + match[0].length;
    }
    paragraph.append(document.createTextNode(text.slice(offset)));
    return paragraph;
  }
  for (const [blockIndex, block] of blocks.entries()) {
    if (block.type === "heading") {
      fragment.append(element("h3", block.text));
    } else if (block.type === "details") {
      const detail = element("details");
      detail.dataset.detailKey = `${detailIndex++}:${block.title}`;
      detail.open = opened.get(detail.dataset.detailKey) || false;
      detail.append(element("summary", block.title), element("p", block.text));
      if (evidencePositions.has(block.title)) {
        if (!evidenceDetails.has(block.title)) evidenceDetails.set(block.title, []);
        evidenceDetails.get(block.title).push({ index: blockIndex, node: detail });
      }
      fragment.append(detail);
    } else if (block.type === "table") {
      const wrapper = element("div");
      wrapper.className = "chat-table";
      wrapper.tabIndex = 0;
      wrapper.setAttribute("role", "region");
      wrapper.setAttribute("aria-label", "交通参考，可横向滚动");
      const table = element("table");
      const head = element("thead");
      const header = element("tr");
      block.header.forEach((cell) => {
        const th = element("th", cell);
        th.scope = "col";
        header.append(th);
      });
      head.append(header);
      table.append(head);
      const body = element("tbody");
      block.rows.forEach((cells) => {
        const row = element("tr");
        cells.forEach((cell) => {
          const td = element("td");
          td.append(...citedParagraph(cell, blockIndex).childNodes);
          row.append(td);
        });
        body.append(row);
      });
      table.append(body);
      wrapper.append(table);
      fragment.append(wrapper);
    } else {
      fragment.append(citedParagraph(block.text, blockIndex));
    }
  }
  container.classList.add("chat-formatted");
  container.replaceChildren(fragment);
  if (focusKey) {
    Array.from(container.querySelectorAll("details")).find((detail) =>
      detail.dataset.detailKey === focusKey)?.querySelector("summary")?.focus({ preventScroll: true });
  }
}

if (typeof module !== "undefined") module.exports = { parseChatText, renderChatText };
