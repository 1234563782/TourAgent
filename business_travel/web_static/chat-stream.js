"use strict";

function chatRunState(run) {
  if (run == null) return { blocked: false, text: "" };
  if (!/^[0-9a-f]{32}$/.test(run.id)) throw new Error("Invalid run identity");
  if (run.status === "running") {
    return { blocked: true, text: `${run.progress?.text || "请求仍在执行"}，可刷新查看进度` };
  }
  if (["failed", "interrupted"].includes(run.status)) {
    return { blocked: true, text: "执行未能确认完成，请核实后建立恢复对话；不会自动重试" };
  }
  if (["completed", "awaiting_confirmation"].includes(run.status)) {
    return { blocked: false, text: "" };
  }
  throw new Error("Invalid run status");
}

async function readChatEvents(response, onEvent) {
  if (!response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new Error("Unexpected response type");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) throw new Error("Incomplete response");
      buffer += decoder.decode(chunk.value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const lines = frame.split("\n");
        const kind = lines.find((line) => line.startsWith("event: "))?.slice(7);
        const dataLines = lines.filter((line) => line.startsWith("data: "));
        if (!dataLines.length) continue;
        const data = JSON.parse(dataLines.map((line) => line.slice(6)).join("\n"));
        if (kind === "error") {
          const error = new Error(typeof data.detail === "string" ? data.detail : "对话执行失败，请刷新核实结果");
          error.status = Number.isInteger(data.status) ? data.status : 503;
          throw error;
        }
        if (kind === "done") {
          if (!Array.isArray(data.messages) || data.messages.some((item) =>
            !["user", "assistant"].includes(item.role) || typeof item.text !== "string")) {
            throw new Error("Invalid committed response");
          }
          return data;
        }
        if ((kind === "delta" || kind === "progress") && onEvent) {
          // Let the caller yield between frames so the browser can paint each
          // streamed update instead of batching the whole response visually.
          await onEvent(kind, data);
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
