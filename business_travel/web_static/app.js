"use strict";
const byId = (id) => document.getElementById(id);
let generation = 0;
let signedIn = false;
let chatBusy = false;
let chatRevision = null;
let chatPending = false;
let chatNeedsRefresh = false;
let chatProgress = "";
let chatRunText = "";
let chatRecoverable = false;
let recoveryReview = null;
let recoveryBusy = false;
let conversationId = null;
let conversationKey = null;
let conversations = [];
let nextConversationOffset = null;
const chatDrafts = new Map();

function message(id, text = "") {
  byId(id).textContent = text;
  byId(id).hidden = !text;
}
function addChat(role, text) {
  const row = document.createElement("div");
  row.className = `chat-message ${role}`;
  if (role === "assistant") renderChatText(row, text);
  else row.textContent = text;
  byId("chat-messages").append(row);
  row.scrollIntoView({ block: "nearest" });
  return row;
}
function nextPaint() {
  return new Promise((resolve) => {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(resolve);
    else setTimeout(resolve, 0);
  });
}
function chatControls() {
  byId("chat-form").setAttribute("aria-busy", String(chatBusy));
  byId("chat-submit").disabled = chatBusy || chatPending || chatNeedsRefresh || !conversationId;
  byId("chat-input").disabled = chatBusy || chatPending || chatNeedsRefresh || !conversationId;
  byId("chat-yes").disabled = chatBusy || chatNeedsRefresh;
  byId("chat-no").disabled = chatBusy || chatNeedsRefresh;
  byId("chat-reload").disabled = chatBusy;
  byId("chat-new").disabled = chatBusy;
  byId("chat-recover").hidden = !chatRecoverable;
  byId("chat-recover").disabled = chatBusy || recoveryBusy;
  byId("conversations-more").disabled = chatBusy;
  byId("conversation-list").querySelectorAll("button").forEach((button) => { button.disabled = chatBusy; });
  byId("chat-status").textContent = chatBusy ? chatProgress || "正在处理…" :
    chatRunText || (chatNeedsRefresh ? "请刷新会话核实执行结果" : "");
}
function renderChat(result) {
  const runState = chatRunState(result.run);
  chatNeedsRefresh = runState.blocked;
  chatRunText = runState.text;
  chatRecoverable = ["failed", "interrupted"].includes(result.run?.status);
  byId("chat-messages").replaceChildren();
  result.messages.forEach((item) => addChat(item.role, item.text));
  chatRevision = result.revision;
  chatPending = Boolean(result.interrupt?.length);
  byId("chat-confirm").hidden = !chatPending;
  byId("chat-details").textContent = chatPending ? JSON.stringify(result.interrupt, null, 2) : "";
  chatControls();
}
function rememberConversation(id = conversationId) {
  try { sessionStorage.setItem(conversationKey, id); } catch (_) { /* Storage may be disabled. */ }
}
function pendingChatRequest(selected, requestId) {
  const key = `${conversationKey}:request:${selected}`;
  try {
    if (requestId === null) sessionStorage.removeItem(key);
    else if (requestId !== undefined) sessionStorage.setItem(key, requestId);
    return sessionStorage.getItem(key);
  } catch (_) { return null; }
}
function renderConversations() {
  byId("conversation-list").replaceChildren();
  conversations.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.conversationId = item.id;
    button.textContent = item.title;
    button.title = item.title;
    button.setAttribute("aria-current", String(item.id === conversationId));
    const time = document.createElement("time");
    time.dateTime = item.updated_at;
    time.textContent = new Date(item.updated_at).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
    button.append(time);
    button.addEventListener("click", () => loadChat(item.id));
    byId("conversation-list").append(button);
  });
  byId("conversations-more").hidden = nextConversationOffset === null;
  byId("conversation-title").textContent =
    conversations.find((item) => item.id === conversationId)?.title || "对话";
  chatControls();
}
async function loadChat(target = null, create = false, more = false) {
  if (chatBusy) return;
  const current = generation;
  let saved = null;
  try { saved = sessionStorage.getItem(conversationKey); } catch (_) { /* Storage may be disabled. */ }
  let selected = target || conversationId || saved;
  const previous = conversations.find((item) => item.id === conversationId);
  if (!more) {
    if (conversationId) chatDrafts.set(conversationId, byId("chat-input").value);
    // A failed switch must not leave an old thread available for sending.
    conversationId = null;
    byId("chat-input").value = "";
    renderChat({ messages: [], revision: null, interrupt: [] });
  }
  chatBusy = true;
  chatControls();
  message("chat-error");
  try {
    if (create) {
      const created = await api("/api/conversations", { method: "POST" });
      if (current !== generation) return;
      selected = created.id;
      rememberConversation(selected);
    }
    const list = await api(`/api/conversations?offset=${more ? nextConversationOffset : 0}`);
    if (current !== generation) return;
    conversations = more ? [...new Map([...conversations, ...list.items].map((item) => [item.id, item])).values()]
      : list.items;
    nextConversationOffset = list.next_offset;
    if (more && previous && !conversations.some((item) => item.id === previous.id)) {
      conversations.push(previous);
    }
    renderConversations();
    if (more) return;
    selected = selected || conversations[0]?.id;
    if (!selected) return;
    const status = await api(`/api/conversations/${encodeURIComponent(selected)}/runs`);
    if (current !== generation) return;
    chatRunState(status.run);
    if (status.run?.status === "running") {
      conversationId = selected;
      rememberConversation();
      renderChat({ messages: [], revision: null, interrupt: [], run: status.run });
      renderConversations();
      return;
    }
    const result = await api(`/api/conversations/${encodeURIComponent(selected)}/chat`);
    if (current !== generation) return;
    chatRunState(result.run);
    if (result.conversation_id !== selected ||
        (["failed", "interrupted"].includes(status.run?.status) &&
         (result.run?.id !== status.run.id || result.run?.status !== status.run.status))) {
      const error = new Error("会话状态不一致，请刷新核实，暂不能继续发送");
      error.status = 503;
      throw error;
    }
    conversationId = selected;
    if (result.run?.id === pendingChatRequest(selected) && result.run.status !== "running") {
      chatDrafts.delete(selected);
      pendingChatRequest(selected, null);
    }
    if (result.conversation && !conversations.some((item) => item.id === selected)) {
      conversations.push(result.conversation);
    }
    rememberConversation();
    byId("chat-input").value = chatDrafts.get(selected) || "";
    renderChat(result);
    renderConversations();
  } catch (error) {
    if (current !== generation) return;
    if (error.status === 401) showLogin("登录已失效，请重新登录");
    else message("chat-error", error.status ? error.message : "无法加载会话，请重试");
  } finally {
    if (current === generation) { chatBusy = false; chatControls(); }
  }
}
async function sendChat(payload) {
  if (chatBusy || chatNeedsRefresh || !conversationId) return;
  const current = generation;
  const selected = conversationId;
  chatBusy = true;
  chatProgress = "";
  const requestId = crypto.randomUUID().replaceAll("-", "");
  pendingChatRequest(selected, requestId);
  let completed = false;
  chatControls();
  message("chat-error");
  const userRow = payload.message ? addChat("user", payload.message) : null;
  const streamingRow = addChat("assistant", "");
  let streamingText = "";
  streamingRow.setAttribute("aria-label", "尚未确认完成的回答");
  try {
    const result = await api(`/api/conversations/${encodeURIComponent(selected)}/chat/stream`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, request_id: requestId }),
    }, async (kind, data) => {
      if (current !== generation || selected !== conversationId) return;
      if (data.run_id !== requestId) throw new Error("Run mismatch");
      if (kind === "progress" && typeof data.text === "string") {
        chatProgress = data.text;
        chatControls();
      } else if (kind === "delta" && typeof data.text === "string") {
        streamingText += data.text;
        renderChatText(streamingRow, streamingText, { streaming: true });
        streamingRow.scrollIntoView({ block: "nearest" });
        await nextPaint();
      }
    });
    if (current !== generation) return;
    if (result.conversation_id !== selected) throw new Error("Conversation mismatch");
    if (result.run?.id !== requestId ||
        !["completed", "awaiting_confirmation"].includes(result.run.status)) {
      throw new Error("Incomplete run");
    }
    completed = true;
    pendingChatRequest(selected, null);
    renderChat(result);
    byId("chat-input").value = "";
    chatDrafts.delete(selected);
    const list = await api("/api/conversations");
    if (current !== generation) return;
    conversations = list.items;
    nextConversationOffset = list.next_offset;
    renderConversations();
  } catch (error) {
    if (current !== generation) return;
    if (error.status === 401) showLogin("登录已失效，请重新登录");
    else if (error.status === 429 && error.admissionRejected) {
      pendingChatRequest(selected, null);
      streamingRow.remove();
      userRow?.remove();
      chatNeedsRefresh = false;
      message("chat-error", error.message);
    }
    else {
      chatNeedsRefresh = !completed;
      message("chat-error", completed ? "回答已收到，但记录刷新失败，请刷新核实。" :
        error.status ? `${error.message}。请刷新会话核实结果，不要重复发送。` :
          "对话未完整接收，请刷新会话核实结果，不要重复发送");
    }
  } finally {
    if (current === generation) { chatBusy = false; chatProgress = ""; chatControls(); }
  }
}
byId("chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = byId("chat-input");
  const text = input.value.trim();
  if (!text || chatBusy || chatPending) return;
  sendChat({ message: text });
});
byId("chat-yes").addEventListener("click", () => {
  sendChat({ resume: true, revision: chatRevision });
});
byId("chat-no").addEventListener("click", () => {
  sendChat({ resume: false, revision: chatRevision });
});
byId("chat-reload").addEventListener("click", () => loadChat());
byId("chat-new").addEventListener("click", () => loadChat(null, true));
byId("conversations-more").addEventListener("click", () => loadChat(null, false, true));
function recoveryControls() {
  byId("recovery-submit").disabled = recoveryBusy || !recoveryReview ||
    (!recoveryReview.recovery && !byId("recovery-reviewed").checked);
  byId("recovery-cancel").disabled = recoveryBusy;
  byId("recovery-reviewed").disabled = recoveryBusy;
}
async function reviewRecovery() {
  if (chatBusy || recoveryBusy || !conversationId) return;
  const current = generation;
  const selected = conversationId;
  recoveryReview = null;
  recoveryBusy = true;
  byId("recovery-reviewed").checked = false;
  message("chat-error");
  chatControls();
  try {
    const review = await api(`/api/conversations/${encodeURIComponent(selected)}/recovery`);
    if (current !== generation || selected !== conversationId) return;
    recoveryReview = { ...review, sourceConversation: selected };
    byId("recovery-notice").textContent = review.recovery
      ? "此执行已建立恢复对话，原请求未重试。" : review.notice;
    byId("recovery-source").textContent = review.recovery ? "" : review.source_checkpoint_id
      ? `继承最近完整历史中的 ${review.history_message_count} 条消息。`
      : "没有可继承的完整轮次，将建立空白恢复对话。";
    byId("recovery-trips").textContent = review.recovery ? "已核实" :
      review.saved_trips.length ? JSON.stringify(review.saved_trips, null, 2) : "暂无";
    byId("recovery-reviewed").parentElement.hidden = Boolean(review.recovery);
    byId("recovery-submit").textContent = review.recovery ? "打开恢复对话" : "确认并建立恢复对话";
    message("recovery-error");
    byId("recovery-dialog").showModal();
  } catch (error) {
    if (current !== generation || selected !== conversationId) return;
    if (error.status === 401) showLogin("登录已失效，请重新登录");
    else message("chat-error", error.status ? error.message : "核实失败，请刷新后重试");
  } finally {
    if (current === generation) { recoveryBusy = false; recoveryControls(); chatControls(); }
  }
}
async function confirmRecovery() {
  if (recoveryBusy || !recoveryReview ||
      (!recoveryReview.recovery && !byId("recovery-reviewed").checked)) return;
  const current = generation;
  const review = recoveryReview;
  recoveryBusy = true;
  recoveryControls();
  message("recovery-error");
  let target = null;
  try {
    const result = review.recovery || await api(
      `/api/conversations/${encodeURIComponent(review.sourceConversation)}/recovery`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: review.run.id, review_revision: review.review_revision, confirmed: true }),
      });
    if (current !== generation) return;
    target = result.conversation_id;
    byId("recovery-dialog").close();
    recoveryReview = null;
  } catch (error) {
    if (current !== generation) return;
    if (error.status === 401) showLogin("登录已失效，请重新登录");
    else {
      message("recovery-error", error.status ? error.message :
        "响应未完整接收，请取消后重新核实；不会重复建立恢复对话");
      // Stale reviews must be read again, not silently reconfirmed.
      if (error.status === 409) recoveryReview = null;
    }
  } finally {
    if (current === generation) { recoveryBusy = false; recoveryControls(); chatControls(); }
  }
  if (target && current === generation) await loadChat(target);
}
byId("chat-recover").addEventListener("click", reviewRecovery);
byId("recovery-reviewed").addEventListener("change", recoveryControls);
byId("recovery-submit").addEventListener("click", confirmRecovery);
byId("recovery-cancel").addEventListener("click", () => {
  if (!recoveryBusy) { byId("recovery-dialog").close(); recoveryReview = null; }
});
byId("recovery-dialog").addEventListener("cancel", (event) => {
  if (recoveryBusy) event.preventDefault();
  else recoveryReview = null;
});
async function api(path, options = {}, stream = false) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(typeof body.detail === "string" ? body.detail : "请求失败，请稍后重试");
    error.status = response.status;
    error.admissionRejected = response.headers.get("X-Travel-Admission") === "rejected";
    throw error;
  }
  if (response.status === 204) return null;
  if (!stream) return response.json();
  return readChatEvents(response, stream);
}
function showLogin(text = "") {
  generation++;
  signedIn = false;
  chatBusy = false;
  chatPending = false;
  chatRevision = null;
  chatNeedsRefresh = false;
  chatProgress = "";
  chatRunText = "";
  chatRecoverable = false;
  recoveryReview = null;
  recoveryBusy = false;
  byId("recovery-dialog").close();
  ["recovery-notice", "recovery-source", "recovery-trips"].forEach(
    (id) => { byId(id).textContent = ""; });
  conversationId = null;
  conversationKey = null;
  conversations = [];
  chatDrafts.clear();
  nextConversationOffset = null;
  renderConversations();
  byId("chat-messages").replaceChildren();
  byId("chat-input").value = "";
  byId("chat-details").textContent = "";
  byId("chat-confirm").hidden = true;
  message("chat-error");
  chatControls();
  ["account-name", "company", "user-id"].forEach((id) => { byId(id).textContent = ""; });
  byId("workspace").hidden = true;
  byId("login-view").hidden = false;
  byId("boot").hidden = true;
  byId("password").value = "";
  byId("password").type = "password";
  byId("show-password").checked = false;
  message("login-error", text);
}
async function restore() {
  const current = generation;
  try {
    const user = await api("/api/auth/me");
    if (current !== generation) return;
    signedIn = true;
    conversationKey = `aligo:conversation:${user.company_id}:${user.user_id}`;
    byId("account-name").textContent = user.username;
    byId("company").textContent = user.company_id;
    byId("user-id").textContent = user.user_id;
    byId("login-view").hidden = true;
    byId("workspace").hidden = false;
    byId("boot").hidden = true;
    await loadChat();
  } catch (error) {
    if (current === generation) showLogin(error.status === 401 ? "" : "服务暂不可用，请稍后重试");
  }
}
byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("login-submit");
  button.disabled = true;
  button.textContent = "正在登录…";
  message("login-error");
  try {
    await api("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: byId("username").value, password: byId("password").value }),
    });
    byId("password").value = "";
    await restore();
  } catch (error) {
    byId("password").value = "";
    message("login-error", error.status ? error.message : "连接失败，请检查网络后重试");
  } finally {
    button.disabled = false;
    button.textContent = "登录";
  }
});
byId("show-password").addEventListener("change", (event) => {
  byId("password").type = event.target.checked ? "text" : "password";
});
byId("logout").addEventListener("click", async () => {
  byId("logout").disabled = true;
  try {
    await api("/api/auth/logout", { method: "POST" });
    showLogin();
    byId("username").focus();
  } catch (error) {
    message("chat-error", "退出未完成，请重试");
  } finally {
    byId("logout").disabled = false;
  }
});
async function checkSession() {
  if (!signedIn || document.hidden) return;
  const current = generation;
  try { await api("/api/auth/me"); }
  catch (error) {
    if (error.status === 401 && current === generation) showLogin("登录已失效，请重新登录");
  }
}
window.addEventListener("focus", checkSession);
window.setInterval(checkSession, 60000);
restore();
