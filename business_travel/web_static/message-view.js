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
  function element(tag, content) {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    return node;
  }
  for (const block of parseChatText(text, streaming)) {
    if (block.type === "heading") {
      fragment.append(element("h3", block.text));
    } else if (block.type === "details") {
      const detail = element("details");
      detail.dataset.detailKey = `${detailIndex++}:${block.title}`;
      detail.open = opened.get(detail.dataset.detailKey) || false;
      detail.append(element("summary", block.title), element("p", block.text));
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
        cells.forEach((cell) => row.append(element("td", cell)));
        body.append(row);
      });
      table.append(body);
      wrapper.append(table);
      fragment.append(wrapper);
    } else {
      fragment.append(element("p", block.text));
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
