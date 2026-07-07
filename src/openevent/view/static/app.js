const form = document.getElementById("queryForm");
const principalInput = document.getElementById("principalInput");
const tokenInput = document.getElementById("tokenInput");
const channelInput = document.getElementById("channelInput");
const recipientInput = document.getElementById("recipientInput");
const statusText = document.getElementById("statusText");
const resultMeta = document.getElementById("resultMeta");
const messageList = document.getElementById("messageList");
const nextButton = document.getElementById("nextButton");
const searchButton = document.getElementById("searchButton");

let nextCursor = null;
let lastQuery = null;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  nextCursor = null;
  messageList.innerHTML = "";
  lastQuery = buildQuery(null);
  await fetchMessages(lastQuery, false);
});

nextButton.addEventListener("click", async () => {
  if (!lastQuery || !nextCursor) return;
  const query = { ...lastQuery, cursor: nextCursor };
  await fetchMessages(query, true);
});

function buildQuery(cursor) {
  const query = {
    principal: principalInput.value.trim(),
    token: tokenInput.value,
    cursor,
    order: "desc",
  };
  const channel = channelInput.value.trim();
  if (channel) query.channel_id = channel;
  if (recipientInput.checked) query.only_my_recipient = true;
  return query;
}

async function fetchMessages(query, append) {
  setBusy(true);
  try {
    const response = await fetch("/v1/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(query),
    });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body.error?.message || `HTTP ${response.status}`);
    }
    nextCursor = body.next_cursor || null;
    nextButton.disabled = !body.has_more || !nextCursor;
    renderMessages(body.messages || [], append, Boolean(body.has_more));
    const count = body.messages ? body.messages.length : 0;
    resultMeta.textContent = `${append ? "Appended" : "Returned"} ${count} message${count === 1 ? "" : "s"}, scanned ${body.scanned || 0}, more available: ${body.has_more ? "yes" : "no"}`;
    statusText.textContent = "Ready";
  } catch (error) {
    renderError(error.message || String(error), append);
    statusText.textContent = "Error";
    nextButton.disabled = true;
  } finally {
    setBusy(false);
  }
}

function setBusy(isBusy) {
  searchButton.disabled = isBusy;
  nextButton.disabled = isBusy || !nextCursor;
  statusText.textContent = isBusy ? "Loading" : statusText.textContent;
}

function renderMessages(messages, append, hasMore) {
  if (!append) {
    messageList.innerHTML = "";
  }
  if (!messages.length && !append) {
    messageList.innerHTML = hasMore
      ? '<div class="empty-state">No visible messages in this scan window. Use Next Page to scan older history.</div>'
      : '<div class="empty-state">No visible messages found.</div>';
    return;
  }
  for (const message of messages) {
    messageList.appendChild(renderMessage(message));
  }
}

function renderError(message, append) {
  if (!append) {
    messageList.innerHTML = "";
  }
  const node = document.createElement("div");
  node.className = "error-state";
  node.textContent = message;
  messageList.prepend(node);
}

function renderMessage(message) {
  const article = document.createElement("article");
  article.className = "message-card";
  article.appendChild(renderSummary(message));
  article.appendChild(renderPayload(message.payload));
  return article;
}

function renderSummary(message) {
  const summary = document.createElement("div");
  summary.className = "message-summary";
  const fields = [
    ["seq", message.seq],
    ["channel", formatChannel(message)],
    ["time", formatTime(message.ts_ms)],
    ["principal", message.principal],
    ["recipients", (message.recipients || []).join(", ") || "[]"],
  ];
  for (const [name, value] of fields) {
    const field = document.createElement("div");
    field.className = "field";
    field.innerHTML = `<span class="field-name"></span><span class="field-value"></span>`;
    field.querySelector(".field-name").textContent = name;
    field.querySelector(".field-value").textContent = String(value);
    summary.appendChild(field);
  }
  summary.appendChild(renderMessageActions(message));
  return summary;
}

function renderMessageActions(message) {
  const actions = document.createElement("div");
  actions.className = "message-actions";
  const copyButton = document.createElement("button");
  copyButton.className = "copy-button";
  copyButton.type = "button";
  copyButton.title = "Copy message JSON";
  copyButton.setAttribute("aria-label", "Copy message JSON");
  copyButton.innerHTML = `
    <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24">
      <rect x="9" y="9" width="10" height="10" rx="2"></rect>
      <path d="M5 15V7a2 2 0 0 1 2-2h8"></path>
    </svg>
  `;
  copyButton.addEventListener("click", async () => {
    await copyMessageJson(message, copyButton);
  });
  actions.appendChild(copyButton);
  return actions;
}

async function copyMessageJson(message, button) {
  button.disabled = true;
  button.classList.remove("is-copied", "is-failed");
  try {
    await copyText(JSON.stringify(message, null, 2));
    button.classList.add("is-copied");
    button.title = "Copied";
    button.setAttribute("aria-label", "Copied");
    statusText.textContent = "Copied";
    setTimeout(() => {
      button.classList.remove("is-copied");
      button.title = "Copy message JSON";
      button.setAttribute("aria-label", "Copy message JSON");
      button.disabled = false;
    }, 1200);
  } catch (error) {
    button.classList.add("is-failed");
    button.title = "Copy failed";
    button.setAttribute("aria-label", "Copy failed");
    statusText.textContent = "Copy failed";
    setTimeout(() => {
      button.classList.remove("is-failed");
      button.title = "Copy message JSON";
      button.setAttribute("aria-label", "Copy message JSON");
      button.disabled = false;
    }, 1600);
  }
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    if (!document.execCommand("copy")) {
      throw new Error("copy command failed");
    }
  } finally {
    textarea.remove();
  }
}

function renderPayload(payload) {
  const panel = document.createElement("div");
  panel.className = "payload-panel";
  const title = document.createElement("h2");
  title.className = "payload-title";
  title.textContent = "payload";
  panel.appendChild(title);

  if (payload && payload.json !== null && payload.json !== undefined) {
    const tree = document.createElement("div");
    tree.className = "json-tree";
    tree.appendChild(renderJsonNode(payload.json, "payload"));
    panel.appendChild(tree);
    return panel;
  }

  const text = document.createElement("pre");
  text.className = "payload-text";
  const details = [];
  if (payload?.encoding) details.push(`encoding: ${payload.encoding}`);
  if (payload?.json_error) details.push(payload.json_error);
  if (payload?.truncated) details.push("truncated");
  text.textContent = `${details.join(" | ")}\n\n${payload?.text || ""}`;
  panel.appendChild(text);
  return panel;
}

function renderJsonNode(value, label) {
  if (Array.isArray(value)) {
    const details = document.createElement("details");
    details.open = true;
    const summary = document.createElement("summary");
    summary.innerHTML = `<span class="json-key"></span> <span>[${value.length}]</span>`;
    summary.querySelector(".json-key").textContent = label;
    details.appendChild(summary);
    value.forEach((item, index) => details.appendChild(renderJsonNode(item, String(index))));
    return details;
  }
  if (value && typeof value === "object") {
    const keys = Object.keys(value);
    const details = document.createElement("details");
    details.open = true;
    const summary = document.createElement("summary");
    summary.innerHTML = `<span class="json-key"></span> <span>{${keys.length}}</span>`;
    summary.querySelector(".json-key").textContent = label;
    details.appendChild(summary);
    for (const key of keys) {
      details.appendChild(renderJsonNode(value[key], key));
    }
    return details;
  }
  const line = document.createElement("div");
  const type = value === null ? "null" : typeof value;
  line.innerHTML = `<span class="json-key"></span>: <span></span>`;
  line.querySelector(".json-key").textContent = label;
  const valueNode = line.querySelector("span:last-child");
  valueNode.className = `json-${type}`;
  valueNode.textContent = value === null ? "null" : JSON.stringify(value);
  return line;
}

function formatChannel(message) {
  const id = message.channel_id ?? "";
  const name = message.channel_name;
  if (!name) return String(id);
  return `${id} ${name}`;
}

function formatTime(tsMs) {
  if (!tsMs) return "";
  const date = new Date(Number(tsMs));
  if (Number.isNaN(date.getTime())) return String(tsMs);
  return date.toISOString();
}
