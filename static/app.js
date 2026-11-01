const state = {
  conversationId: Number(localStorage.getItem("supportMateConversationId")) || null,
  conversationToken: localStorage.getItem("supportMateConversationToken") || null,
  customerName: localStorage.getItem("supportMateCustomerName") || "Guest",
  adminAuthenticated: false,
  currentView: "inbox",
  pendingAdminView: null,
  toastTimer: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

async function api(path, options = {}) {
  const { admin = false, ...fetchOptions } = options;
  const method = (fetchOptions.method || "GET").toUpperCase();
  const csrfToken = document.cookie
    .split("; ")
    .find((cookie) => cookie.startsWith("support_mate_csrf="))
    ?.slice("support_mate_csrf=".length);
  const response = await fetch(path, {
    ...fetchOptions,
    headers: {
      ...(fetchOptions.body ? { "Content-Type": "application/json" } : {}),
      ...(csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)
        ? { "X-CSRF-Token": decodeURIComponent(csrfToken) }
        : {}),
      ...fetchOptions.headers,
    },
  });
  if (response.status === 401 && admin) {
    state.adminAuthenticated = false;
    $("#logout-button").hidden = true;
    openAdminLogin(state.currentView);
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch (error) {
      console.error("Could not read the error response.", error);
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function showToast(message, error = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function shortDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

function addMessage(role, content, createdAt = new Date().toISOString()) {
  const messages = $("#chat-messages");
  $(".welcome-message", messages)?.remove();
  const message = document.createElement("article");
  message.className = `message ${role}`;
  const meta = document.createElement("span");
  meta.className = "message-meta";
  meta.textContent = `${role === "customer" ? state.customerName : role === "agent" ? "Support team" : "Support assistant"} · ${formatTime(createdAt)}`;
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = content;
  message.append(meta, bubble);
  messages.append(message);
  messages.scrollTop = messages.scrollHeight;
}

function addEscalationNote(ticketId) {
  const note = document.createElement("div");
  note.className = "escalation-note";
  note.textContent = `Support ticket #${ticketId} created — a team member will follow up.`;
  $("#chat-messages").append(note);
  $("#chat-messages").scrollTop = $("#chat-messages").scrollHeight;
}

function updateConversationDetails(id, status = "Active") {
  $("#conversation-label").textContent = id ? `#${id}` : "New";
  const statusText = $(".status-text");
  statusText.lastChild.textContent = ` ${status}`;
}

function showView(view) {
  if (view !== "inbox" && !state.adminAuthenticated) {
    openAdminLogin(view);
    return;
  }
  state.currentView = view;
  $$(".view-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${view}-view`));
  $$(".nav-link").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  $("#page-name").textContent = { inbox: "Inbox", dashboard: "Dashboard", knowledge: "Knowledge base" }[view] || "Inbox";
  if (view === "dashboard") loadDashboard();
  if (view === "knowledge") loadKnowledge();
}

function openAdminLogin(view = state.currentView) {
  state.pendingAdminView = view;
  const dialog = $("#admin-login-dialog");
  if (!dialog.open) dialog.showModal();
  $("#admin-username").focus();
}

async function loadAdminSession() {
  try {
    const session = await api("/api/auth/session");
    state.adminAuthenticated = session.authenticated;
    $("#logout-button").hidden = !session.authenticated;
    if (session.authenticated && state.currentView !== "inbox") showView(state.currentView);
  } catch (error) {
    console.error("Could not check the admin session.", error);
    showToast(error.message, true);
  }
}

async function restoreConversation() {
  if (!state.conversationId) return;
  try {
    const data = await api(`/api/conversations/${state.conversationId}`, {
      headers: { "X-Conversation-Token": state.conversationToken || "" },
    });
    state.customerName = data.conversation.customer_name;
    $("#customer-name-label").textContent = state.customerName;
    $("#customer-avatar").textContent = state.customerName.charAt(0).toUpperCase();
    updateConversationDetails(state.conversationId, data.conversation.status === "escalated" ? "Needs attention" : data.conversation.status);
    data.messages.forEach((message) => addMessage(message.role, message.content, message.created_at));
  } catch (error) {
    console.error("Could not restore the previous conversation.", error);
    localStorage.removeItem("supportMateConversationId");
    localStorage.removeItem("supportMateConversationToken");
    state.conversationId = null;
    state.conversationToken = null;
  }
}

async function sendMessage(message) {
  const input = $("#chat-input");
  const button = $(".send-button");
  const content = (message ?? input.value).trim();
  if (!content || button.disabled) return;
  state.customerName = (localStorage.getItem("supportMateCustomerName") || "Guest").trim() || "Guest";
  $("#customer-name-label").textContent = state.customerName;
  $("#customer-avatar").textContent = state.customerName.charAt(0).toUpperCase();
  addMessage("customer", content);
  input.value = "";
  button.disabled = true;
  try {
    const result = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        message: content,
        conversation_id: state.conversationId,
        conversation_token: state.conversationToken,
        customer_name: state.customerName,
      }),
    });
    state.conversationId = result.conversation_id;
    state.conversationToken = result.conversation_token;
    localStorage.setItem("supportMateConversationId", String(state.conversationId));
    localStorage.setItem("supportMateConversationToken", state.conversationToken);
    $("#conversation-label").textContent = `#${state.conversationId}`;
    addMessage("assistant", result.answer);
    if (result.escalated && result.ticket_id) {
      updateConversationDetails(state.conversationId, "Needs attention");
      addEscalationNote(result.ticket_id);
      showToast("Your message has been sent to the support team.");
    }
  } catch (error) {
    console.error("Could not send the customer message.", error);
    addMessage("assistant", "Sorry, your message couldn't be sent. Please try again.");
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    input.focus();
  }
}

function createStatusSelect(ticket) {
  const select = document.createElement("select");
  select.className = `ticket-status ticket-select ${ticket.status}`;
  select.setAttribute("aria-label", `Change status for ticket ${ticket.id}`);
  for (const [value, label] of [["open", "Open"], ["in_progress", "In progress"], ["resolved", "Resolved"]]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = ticket.status === value;
    select.append(option);
  }
  select.addEventListener("change", async () => {
    try {
      await api(`/api/tickets/${ticket.id}`, { method: "PATCH", body: JSON.stringify({ status: select.value }), admin: true });
      showToast(`Ticket #${ticket.id} updated.`);
      await loadDashboard();
    } catch (error) {
      showToast(error.message, true);
      await loadDashboard();
    }
  });
  return select;
}

function addTicketRow(ticket) {
  const row = document.createElement("tr");
  const name = document.createElement("td");
  name.textContent = ticket.customer_name;
  const subject = document.createElement("td");
  subject.title = ticket.description;
  subject.textContent = ticket.subject;
  const date = document.createElement("td");
  date.textContent = shortDate(ticket.created_at);
  const status = document.createElement("td");
  status.append(createStatusSelect(ticket));
  const actions = document.createElement("td");
  const openButton = document.createElement("button");
  openButton.className = "ticket-open";
  openButton.textContent = "Reply";
  openButton.addEventListener("click", () => replyToTicket(ticket));
  actions.append(openButton);
  row.append(name, subject, date, status, actions);
  $("#tickets-body").append(row);
}

function addConversationRow(conversation) {
  const row = document.createElement("article");
  row.className = "conversation-row";
  const avatar = document.createElement("div");
  avatar.className = "avatar avatar-peach";
  avatar.textContent = conversation.customer_name.charAt(0).toUpperCase();
  const copy = document.createElement("div");
  copy.className = "conversation-copy";
  const name = document.createElement("strong");
  name.textContent = conversation.customer_name;
  const lastMessage = document.createElement("span");
  lastMessage.textContent = conversation.last_message || "No messages yet";
  const date = document.createElement("span");
  date.className = "conversation-date";
  date.textContent = shortDate(conversation.updated_at);
  copy.append(name, lastMessage);
  row.append(avatar, copy, date);
  $("#conversations-list").append(row);
}

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard", { admin: true });
    $("#stat-conversations").textContent = data.stats.conversations;
    $("#stat-open").textContent = data.stats.open_tickets;
    $("#stat-resolved").textContent = data.stats.resolved_tickets;
    $("#stat-knowledge").textContent = data.stats.knowledge_articles;
    $("#nav-open-count").textContent = data.stats.open_tickets;
    $("#nav-open-count").classList.toggle("visible", data.stats.open_tickets > 0);
    $("#tickets-body").replaceChildren();
    data.tickets.forEach(addTicketRow);
    $("#ticket-count").textContent = `${data.tickets.length} ${data.tickets.length === 1 ? "ticket" : "tickets"}`;
    $("#tickets-empty").classList.toggle("visible", data.tickets.length === 0);
    $("#conversations-list").replaceChildren();
    data.conversations.forEach(addConversationRow);
  } catch (error) {
    console.error("Could not load the support dashboard.", error);
    showToast(error.message, true);
  }
}

async function replyToTicket(ticket) {
  const message = window.prompt(`Reply to ${ticket.customer_name} about "${ticket.subject}":`);
  if (message === null) return;
  if (!message.trim()) {
    showToast("Please enter a reply before sending.", true);
    return;
  }
  const resolve = window.confirm("Mark this ticket as resolved after sending the reply?");
  try {
    await api(`/api/tickets/${ticket.id}/reply`, {
      method: "POST",
      body: JSON.stringify({ message: message.trim(), resolve }),
      admin: true,
    });
    showToast(resolve ? "Reply sent and ticket resolved." : "Reply sent to the customer.");
    await loadDashboard();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadKnowledge() {
  try {
    const entries = await api("/api/knowledge", { admin: true });
    $("#knowledge-count").textContent = `${entries.length} ${entries.length === 1 ? "article" : "articles"}`;
    $("#knowledge-list").replaceChildren();
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "knowledge-empty";
      empty.textContent = "No articles yet. Add your first business answer.";
      $("#knowledge-list").append(empty);
      return;
    }
    for (const entry of entries) {
      const item = document.createElement("article");
      item.className = "knowledge-item";
      const header = document.createElement("div");
      header.className = "knowledge-item-header";
      const copy = document.createElement("div");
      const question = document.createElement("h3");
      question.textContent = entry.question;
      const answer = document.createElement("p");
      answer.textContent = entry.answer;
      copy.append(question, answer);
      const remove = document.createElement("button");
      remove.className = "delete-article";
      remove.textContent = "Remove";
      remove.setAttribute("aria-label", `Remove ${entry.question}`);
      remove.addEventListener("click", async () => {
        if (!window.confirm("Remove this knowledge base article?")) return;
        try {
          await api(`/api/knowledge/${entry.id}`, { method: "DELETE", admin: true });
          showToast("Article removed.");
          await loadKnowledge();
        } catch (error) {
          showToast(error.message, true);
        }
      });
      header.append(copy, remove);
      item.append(header);
      $("#knowledge-list").append(item);
    }
  } catch (error) {
    console.error("Could not load the knowledge base.", error);
    showToast(error.message, true);
  }
}

$$("[data-view]").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$("#chat-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage();
});
$("#chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});
$$(".suggestion").forEach((button) => button.addEventListener("click", () => sendMessage(button.dataset.prompt)));
$("#new-chat-button").addEventListener("click", () => {
  state.conversationId = null;
  state.conversationToken = null;
  localStorage.removeItem("supportMateConversationId");
  localStorage.removeItem("supportMateConversationToken");
  $("#chat-messages").replaceChildren();
  $("#chat-messages").append($(".welcome-message") || createWelcomeMessage());
  updateConversationDetails(null);
  showToast("Started a new conversation.");
  $("#chat-input").focus();
});
$("#refresh-button").addEventListener("click", () => {
  if (!state.adminAuthenticated) {
    openAdminLogin("dashboard");
    return;
  }
  if (state.currentView === "knowledge") loadKnowledge();
  else loadDashboard();
  showToast("Dashboard refreshed.");
});
$("#knowledge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  try {
    await api("/api/knowledge", {
      method: "POST",
      body: JSON.stringify({ question: data.get("question"), answer: data.get("answer") }),
      admin: true,
    });
    form.reset();
    showToast("Answer added to your knowledge base.");
    await loadKnowledge();
  } catch (error) {
    showToast(error.message, true);
  }
});

$("#admin-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const submitButton = $('button[type="submit"]', form);
  submitButton.disabled = true;
  try {
    const session = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: formData.get("username"),
        password: formData.get("password"),
      }),
    });
    state.adminAuthenticated = session.authenticated;
    $("#logout-button").hidden = false;
    form.reset();
    $("#admin-login-dialog").close();
    const view = state.pendingAdminView || state.currentView;
    state.pendingAdminView = null;
    showView(view);
    showToast("Signed in successfully.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    submitButton.disabled = false;
  }
});

$("#cancel-login-button").addEventListener("click", () => {
  $("#admin-login-dialog").close();
  state.pendingAdminView = null;
});

$("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/auth/logout", { method: "POST" });
    state.adminAuthenticated = false;
    $("#logout-button").hidden = true;
    showView("inbox");
    showToast("You have signed out.");
  } catch (error) {
    showToast(error.message, true);
  }
});

function createWelcomeMessage() {
  const welcome = document.createElement("div");
  welcome.className = "welcome-message";
  const icon = document.createElement("div");
  icon.className = "welcome-symbol";
  icon.textContent = "✳";
  const title = document.createElement("h3");
  title.textContent = "Hi there! How can we help?";
  const description = document.createElement("p");
  description.textContent = "Ask us anything about our products, policies, or services.";
  const suggestions = document.createElement("div");
  suggestions.className = "suggestion-list";
  for (const prompt of ["What are your business hours?", "What is your return policy?", "How long does shipping take?"]) {
    const button = document.createElement("button");
    button.className = "suggestion";
    button.dataset.prompt = prompt;
    button.textContent = prompt;
    button.addEventListener("click", () => sendMessage(prompt));
    suggestions.append(button);
  }
  welcome.append(icon, title, description, suggestions);
  return welcome;
}

$("#chat-menu-button").addEventListener("click", () => {
  const name = window.prompt("What should we call you?", state.customerName);
  if (name === null) return;
  state.customerName = name.trim() || "Guest";
  localStorage.setItem("supportMateCustomerName", state.customerName);
  $("#customer-name-label").textContent = state.customerName;
  $("#customer-avatar").textContent = state.customerName.charAt(0).toUpperCase();
});

restoreConversation();
loadAdminSession();
