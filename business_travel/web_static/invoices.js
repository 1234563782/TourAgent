"use strict";
// Originals remain in page memory only; never put invoice data in browser storage.
window.InvoiceReviewUI = (() => {
  const get = (id) => document.getElementById(id);
  const verdicts = { local_checks_passed: "基础检查通过", rejected: "基础检查不通过",
    manual_review: "需人工核实", duplicate: "本系统已见过", unsupported: "格式暂不支持", failed: "本轮未完成" };
  const policyStatuses = { within_checked_rules: "已核对一般差标", over_standard: "超出一般差标",
    insufficient_information: "信息不足", needs_clarification: "待补充信息", manual_review: "需人工核实", failed: "本轮未完成" };
  const fieldNames = { nightly_amounts: "逐晚含税金额 / 水单", check_out: "退房日期",
    expense_date: "实际发生日期", lodging_standard: "账号住宿档次（管理员维护）",
    transport_standard: "账号交通身份（管理员维护）", date_verified: "费用日期与凭证的一致性",
    taxes_confirmed: "含税口径", currency: "币种", amount: "费用金额",
    basis: "金额口径", transport_amount_scope: "交通票价和附加费用明细",
    mainland_business_trip: "是否中国内地出差", seat_class: "座次 / 舱位" };
  function money(value) {
    if (value == null) return "未提供";
    // Keep Decimal strings as strings: large invoice totals must not round through JS Number.
    const parts = String(value).split(".");
    const integer = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return integer + "." + (parts[1] || "").padEnd(2, "0");
  }
  let version = 0, config = null, request = null, expired = null, busy = false;
  function text(tag, value, className) {
    const node = document.createElement(tag); node.textContent = value;
    if (className) node.className = className;
    return node;
  }
  function status(value, error = false) {
    get(error ? "invoice-error" : "invoice-status").textContent = value;
    if (error) get("invoice-error").hidden = !value;
  }
  function controls(value) {
    busy = value;
    get("invoice-form").setAttribute("aria-busy", String(value));
    get("invoice-submit").disabled = value || !config;
    get("invoice-files").disabled = value;
    get("invoice-results").querySelectorAll("button").forEach((node) => { node.disabled = value; });
  }
  function reset() {
    version++; config = null; request = null; expired = null;
    get("invoice-form").reset(); get("invoice-results").replaceChildren();
    get("invoice-limit").textContent = "正在读取上传限制…";
    status("尚未导入票据。可先检查票面，再逐票补充费用信息进行差标测算。");
    status("", true); controls(false);
  }
  async function activate(api, onExpired) {
    reset(); request = api; expired = onExpired;
    const current = version;
    try {
      const result = await api("/api/invoices/config");
      if (current !== version) return;
      config = result;
      get("invoice-limit").textContent = "每批最多 " + config.max_batch_files + " 张，单张不超过 " +
        (config.max_file_bytes / 1048576).toFixed(1) + " MB。暂不支持 PDF、OFD 或扫描图片。" +
        (config.buyer_configured ? "" : " 单位抬头/税号尚未配置，购买方匹配将提示待核实。");
      controls(false);
    } catch (error) {
      if (current !== version) return;
      if (error.status === 401) expired();
      else { get("invoice-limit").textContent = "上传服务暂不可用，请刷新页面重试。"; status(error.message, true); }
    }
  }
  function addFact(list, label, value) {
    const item = document.createElement("div");
    item.append(text("dt", label), text("dd", value ?? "未提供")); list.append(item);
  }
  function field(form, label, type, name, required = false) {
    const wrap = document.createElement("label"); wrap.append(text("span", label));
    const input = document.createElement(type === "textarea" || type === "select" ? type : "input");
    if (input.tagName === "INPUT") input.type = type;
    input.name = name; input.required = required; wrap.append(input); form.append(wrap); return input;
  }
  function options(input, values) {
    values.forEach(([value, label]) => { const option = text("option", label); option.value = value; input.append(option); });
  }
  function supplement(article, result, upload) {
    if (!result.invoice || !["local_checks_passed", "duplicate", "manual_review"].includes(result.verdict)) return;
    const details = document.createElement("details"); details.append(text("summary", "补充费用信息，进行差标测算"));
    details.append(text("p", "仅关联本张票的完整含税金额，不自动分摊。住宿总额不能均摊成每晚；缺明细时请补水单。身份使用账号配置，不在此填写。", "invoice-help"));
    const form = document.createElement("form"); form.className = "invoice-expense";
    const kind = field(form, "科目", "select", "kind", true);
    options(kind, [["hotel", "住宿"], ["transport", "城际交通"]]);
    field(form, "实际发生日期 / 入住日期（不是开票日）", "date", "expense_date", true);
    const destination = field(form, "费用所在城市", "text", "destination", true); destination.maxLength = 100;
    const province = field(form, "所在省份（可选）", "text", "destination_province"); province.maxLength = 100;
    const hotel = document.createElement("div"); hotel.className = "invoice-kind-fields";
    field(hotel, "退房日期（不含当天）", "date", "check_out", true);
    const occupants = field(hotel, "每间入住人数", "number", "occupants", true);
    occupants.min = "1"; occupants.max = "20"; occupants.value = "1";
    const nightly = field(hotel, "逐晚含税金额（可选，每行：2026-10-01,500.00）", "textarea", "nightly");
    nightly.rows = 3; nightly.maxLength = 16000; form.append(hotel);
    const transport = document.createElement("div"); transport.className = "invoice-kind-fields"; transport.hidden = true;
    const mode = field(transport, "交通方式", "select", "transport_mode");
    options(mode, [["high_speed_train", "高铁 / 动车"], ["regular_train", "普速列车"], ["flight", "飞机"]]);
    const seat = field(transport, "座次 / 舱位", "text", "seat_class"); seat.maxLength = 50;
    const fare = field(transport, "金额仅为票价（不包含保险、服务费等）", "checkbox", "fare_only"); form.append(transport);
    const switchKind = () => {
      hotel.hidden = kind.value !== "hotel"; transport.hidden = !hotel.hidden;
      hotel.querySelectorAll("input,textarea").forEach((node) => { node.disabled = hotel.hidden; });
      transport.querySelectorAll("input,select").forEach((node) => { node.disabled = transport.hidden; });
    };
    kind.addEventListener("change", switchKind); switchKind();
    const previous = upload.expenses?.[0]?.facts;
    if (previous) {
      form.querySelectorAll("input,select,textarea").forEach((node) => {
        if (previous[node.name] != null) node.value = String(previous[node.name]);
      });
      nightly.value = (previous.nightly_amounts || []).map((item) => item.date + "," + item.amount).join(String.fromCharCode(10));
      fare.checked = previous.transport_amount_scope === "fare_only"; switchKind();
    }
    const button = text("button", "按补充信息重新测算"); button.type = "submit"; form.append(button);
    const errorNode = text("p", "", "error"); errorNode.hidden = true; form.append(errorNode);
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); if (busy || !request) return;
      const current = version; errorNode.hidden = true;
      try {
        const values = Object.fromEntries(new FormData(form));
        const facts = { kind: values.kind, expense_date: values.expense_date, destination: values.destination.trim(),
          destination_province: values.destination_province.trim() || null, amount: result.invoice.amount_total,
          currency: "CNY", taxes_confirmed: true, basis: values.kind === "hotel" ? "room_night" : "ticket" };
        if (values.kind === "hotel") {
          facts.check_out = values.check_out || null; facts.amount_scope = values.check_out ? "stay_total" : "single";
          facts.occupants = Number(values.occupants);
          if (values.nightly.trim()) {
            facts.nightly_amounts = values.nightly.trim().split(/\r?\n/).map((line) => {
              const match = line.trim().match(/^(\d{4}-\d{2}-\d{2})[,，]\s*(\d+(?:\.\d{1,2})?)$/);
              if (!match) throw new Error("逐晚金额格式应为：2026-10-01,500.00，每晚单独一行。");
              return { date: match[1], amount: match[2] };
            });
            facts.amount_scope = "stay_total";
          }
        } else { facts.transport_mode = values.transport_mode; facts.seat_class = values.seat_class;
          facts.transport_amount_scope = fare.checked ? "fare_only" : "unknown"; }
        controls(true);
        const updatedUpload = { ...upload, expenses: [{line_id: "1", facts}] };
        const batch = await request("/api/invoices/review", { method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ files: [updatedUpload] }) });
        if (current !== version) return;
        article.replaceWith(render(batch.results[0], updatedUpload)); status("已按补充费用信息重新测算；请查看该票的差标结果及待核实项。");
      } catch (error) {
        if (current !== version) return;
        if (error.status === 401) expired(); else { errorNode.textContent = error.message; errorNode.hidden = false; }
      } finally { if (current === version) controls(false); }
    });
    details.append(form); article.append(details);
  }
  function render(result, upload) {
    const article = document.createElement("article"); article.className = "invoice-result";
    const heading = document.createElement("div"); heading.className = "invoice-heading";
    heading.append(text("h3", result.filename), text("strong", verdicts[result.verdict] || "待核实", "invoice-verdict " + result.verdict)); article.append(heading);
    if (result.invoice) {
      const invoice = result.invoice, facts = document.createElement("dl"); facts.className = "invoice-facts";
      addFact(facts, "发票号码", invoice.number); addFact(facts, "开票日期", invoice.issue_date);
      addFact(facts, "价税合计（CNY）", money(invoice.amount_total)); addFact(facts, "购买方", invoice.buyer_name);
      addFact(facts, "销售方", invoice.seller_name); article.append(facts);
    }
    const reasons = document.createElement("ul");
    result.reasons.forEach((reason) => {
      const item = text("li", reason.detail + " [" + reason.code + "]");
      if (reason.evidence) item.append(text("small", "字段：" + reason.field + " · XML：" + reason.evidence.xml_path));
      reasons.append(item);
    }); article.append(reasons);
    if (result.attachment_kind === "xml_other") {
      article.append(text("p", "这是 XML 文件，但不是当前支持的数电票结构；未登记发票、未查重，也未进入差标。若文字中已提供完整费用事实，可继续按文字核对。", "invoice-help"));
    }
    (result.policy_lines || []).forEach((line) => {
      const block = document.createElement("div"); block.className = "invoice-policy";
      block.append(text("h4", "费用行 " + line.line_id + " · 差标：" + (policyStatuses[line.status] || line.status)));
      const estimate = line.policy_result?.estimate;
      if (estimate?.reference_amount != null) block.append(text("p", "按补充事实测算的参考金额：" + money(estimate.reference_amount) + " " + estimate.currency + "；不是票据验真或报销批准。"));
      if (line.missing_fields?.length) block.append(text("p", "还需补充 / 核实：" + line.missing_fields.map((name) => fieldNames[name] || name).join("、") + "。"));
      if (line.detail) block.append(text("p", line.detail));
      block.append(text("p", line.notice || "请按待补项核实；不要将未完成视为可报销。", "invoice-help"));
      const raw = document.createElement("details"); raw.append(text("summary", "查看差标依据与理由码"));
      raw.append(text("pre", JSON.stringify(line.policy_result || line, null, 2))); block.append(raw); article.append(block);
    });
    if (result.invoice) {
      const proof = document.createElement("details"); proof.append(text("summary", "查看字段来源"));
      proof.append(text("pre", JSON.stringify({ file_hash: result.file_hash, evidence: result.invoice.evidence,
        signature_status: result.invoice.signature_status, verification_status: result.verification_status }, null, 2))); article.append(proof);
    }
    article.append(text("p", result.notice, "invoice-help")); supplement(article, result, upload); return article;
  }
  get("invoice-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (busy || !config || !request) return;
    const current = version, selected = [...get("invoice-files").files]; status("", true);
    try {
      if (!selected.length || selected.length > config.max_batch_files) throw new Error("请选择 1–" + config.max_batch_files + " 个文件。");
      if (selected.some((file) => file.size > config.max_file_bytes)) throw new Error("有文件超过大小限制，请移除后重试。");
      controls(true); status("正在逐票预审…"); const uploads = [];
      for (const file of selected) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = ""; for (const byte of bytes) binary += String.fromCharCode(byte);
        uploads.push({filename: file.name, content_base64: btoa(binary), expenses: []});
      }
      if (current !== version) return;
      const result = await request("/api/invoices/review", { method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({files: uploads}) });
      if (current !== version) return;
      get("invoice-results").replaceChildren(...result.results.map((item, index) => render(item, uploads[index])));
      const completed = result.results.filter((item) => !["failed", "unsupported"].includes(item.verdict)).length;
      status("本轮 " + result.results.length + " 张：" + completed + " 张已返回预审意见，" + (result.results.length - completed) + " 张未完成或格式不支持。请逐票查看；未进行官方验真。");
    } catch (error) {
      if (current !== version) return;
      if (error.status === 401) expired();
      else { status(error.message, true); status("本轮未完成，请修正问题后重试；上轮结果仍保留。"); }
    } finally { if (current === version) controls(false); }
  });
  return {activate, reset};
})();
