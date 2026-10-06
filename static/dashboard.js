const dashboard = document.querySelector("#dashboard");

if (dashboard) {
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const money = cents => new Intl.NumberFormat("en-KE", {
    style: "currency", currency: "KES", minimumFractionDigits: 2
  }).format(cents / 100);
  const colors = ["#c9f36b", "#f2b85d", "#ff8874", "#6dd6b2", "#e889b5", "#c0a1e8", "#a6d96a"];
  const rows = document.querySelector("#transaction-rows");
  let summary;

  async function requestJson(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("X-CSRF-Token", csrfToken);
    headers.set("Content-Type", "application/json");
    const response = await fetch(url, { ...options, headers });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
    return data;
  }

  function drawDonut(items) {
    const canvas = document.querySelector("#purpose-chart");
    const empty = document.querySelector("#purpose-empty");
    const legend = document.querySelector("#purpose-legend");
    legend.replaceChildren();
    const context = canvas.getContext("2d");
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const total = items.reduce((sum, item) => sum + item.total, 0);
    empty.hidden = total > 0;
    if (!total) return;
    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.39;
    let angle = -Math.PI / 2;
    items.slice(0, 8).forEach((item, index) => {
      const slice = item.total / total * Math.PI * 2;
      context.beginPath();
      context.arc(centerX, centerY, radius, angle, angle + slice - 0.025);
      context.strokeStyle = colors[index % colors.length];
      context.lineWidth = Math.max(16, radius * 0.27);
      context.stroke();
      angle += slice;
      const entry = document.createElement("li");
      const swatch = document.createElement("i");
      const label = document.createElement("span");
      const amount = document.createElement("strong");
      swatch.style.backgroundColor = colors[index % colors.length];
      label.textContent = item.purpose;
      amount.textContent = money(item.total);
      entry.append(swatch, label, amount);
      legend.append(entry);
    });
    context.fillStyle = "#e5ecd7";
    context.font = "700 13px Manrope, sans-serif";
    context.textAlign = "center";
    context.fillText("TOTAL USED", centerX, centerY - 9);
    context.font = "500 15px monospace";
    context.fillText(money(total), centerX, centerY + 13);
  }

  function drawTrend(months) {
    const canvas = document.querySelector("#trend-chart");
    const empty = document.querySelector("#trend-empty");
    const context = canvas.getContext("2d");
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const values = new Map(months.map(item => [`${item.month}:${item.direction}`, item.total]));
    const today = new Date();
    const series = Array.from({ length: 6 }, (_, index) => {
      const date = new Date(today.getFullYear(), today.getMonth() - 5 + index, 1);
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
      return {
        label: date.toLocaleString(undefined, { month: "short" }),
        received: values.get(`${key}:received`) || 0,
        used: values.get(`${key}:used`) || 0
      };
    });
    empty.hidden = series.some(item => item.received || item.used);
    const max = Math.max(1, ...series.flatMap(item => [item.received, item.used]));
    const bottom = height - 26;
    const plotHeight = bottom - 12;
    const groupWidth = width / series.length;
    const barWidth = Math.min(16, groupWidth * 0.24);
    series.forEach((item, index) => {
      const center = groupWidth * (index + 0.5);
      context.fillStyle = colors[0];
      context.fillRect(center - barWidth - 2, bottom - item.received / max * plotHeight, barWidth, item.received / max * plotHeight);
      context.fillStyle = colors[1];
      context.fillRect(center + 2, bottom - item.used / max * plotHeight, barWidth, item.used / max * plotHeight);
      context.fillStyle = "#a3af9a";
      context.font = "11px Manrope, sans-serif";
      context.textAlign = "center";
      context.fillText(item.label, center, height - 7);
    });
  }

  function drawTransactions(transactions) {
    rows.replaceChildren();
    if (!transactions.length) {
      rows.innerHTML = '<tr><td colspan="5" class="empty-row">Your first entry will show up here.</td></tr>';
      return;
    }
    transactions.forEach(transaction => {
      const row = document.createElement("tr");
      const type = document.createElement("td");
      const purpose = document.createElement("td");
      const date = document.createElement("td");
      const amount = document.createElement("td");
      const actions = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = `transaction-type ${transaction.direction === "received" ? "type-received" : "type-used"}`;
      badge.textContent = transaction.direction === "received" ? "Received" : "Used";
      type.append(badge);
      purpose.textContent = transaction.purpose;
      date.textContent = new Date(transaction.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
      amount.className = `amount-col ${transaction.direction === "received" ? "money-positive" : "money-negative"}`;
      amount.textContent = `${transaction.direction === "received" ? "+" : "−"}${money(transaction.amount_cents)}`;
      const remove = document.createElement("button");
      remove.className = "delete-transaction";
      remove.type = "button";
      remove.textContent = "Delete";
      remove.setAttribute("aria-label", `Delete ${transaction.purpose}`);
      remove.addEventListener("click", async () => {
        if (!window.confirm("Delete this transaction? This cannot be undone.")) return;
        try {
          await requestJson(`/api/transactions/${transaction.id}`, { method: "DELETE" });
          await refresh();
        } catch (error) {
          showNotice(error.message, true);
        }
      });
      actions.append(remove);
      row.append(type, purpose, date, amount, actions);
      rows.append(row);
    });
  }

  function drawLeaderboard(data) {
    const leaderboardRows = document.querySelector("#leaderboard-rows");
    const position = document.querySelector("#leaderboard-position");
    leaderboardRows.replaceChildren();
    data.leaders.forEach(leader => {
      const row = document.createElement("tr");
      const rank = document.createElement("td");
      const username = document.createElement("td");
      const points = document.createElement("td");
      rank.textContent = `#${leader.rank}`;
      username.textContent = leader.username;
      points.className = "amount-col";
      points.textContent = leader.points.toLocaleString();
      row.append(rank, username, points);
      leaderboardRows.append(row);
    });
    if (!data.leaders.length) {
      leaderboardRows.innerHTML = '<tr><td colspan="3" class="empty-row">No rankings yet.</td></tr>';
    }
    position.textContent = data.current_user
      ? `Your rank: #${data.current_user.rank} · ${data.current_user.points.toLocaleString()} points`
      : "";
  }

  function render(data) {
    summary = data;
    document.querySelector("#total-received").textContent = money(data.received);
    document.querySelector("#total-spent").textContent = money(data.spent);
    document.querySelector("#total-balance").textContent = money(data.balance);
    const progress = document.querySelector("#budget-progress");
    const input = document.querySelector("#budget-amount");
    if (data.monthly_budget) {
      const percent = Math.round(data.monthly_spent / data.monthly_budget * 100);
      document.querySelector("#budget-message").textContent =
        `${money(data.monthly_spent)} of ${money(data.monthly_budget)} used this month${percent > 100 ? " · Budget exceeded" : ` · ${percent}%`}`;
      progress.style.width = `${Math.min(percent, 100)}%`;
      progress.classList.toggle("is-over", percent >= 90);
      input.value = (data.monthly_budget / 100).toFixed(2);
    } else {
      document.querySelector("#budget-message").textContent = "Set a monthly budget to get started.";
      progress.style.width = "0%";
    }
    drawDonut(data.purposes);
    drawTrend(data.months);
    drawTransactions(data.transactions);
  }

  function showNotice(message, isError = false) {
    const notice = document.querySelector("#form-notice");
    notice.textContent = message;
    notice.classList.toggle("is-error", isError);
  }

  async function refresh() {
    try {
      render(await requestJson("/api/summary", { headers: {} }));
      const leaders = await requestJson("/api/leaderboard", { headers: {} });
      drawLeaderboard(leaders);
    } catch (error) {
      showNotice(error.message, true);
    }
  }

  document.querySelectorAll(".segment").forEach(button => {
    button.addEventListener("click", () => {
      document.querySelectorAll(".segment").forEach(item => {
        const selected = item === button;
        item.classList.toggle("is-selected", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
      document.querySelector("#direction").value = button.dataset.direction;
    });
  });

  document.querySelector("#transaction-form").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button[type='submit']");
    submit.disabled = true;
    try {
      await requestJson("/api/transactions", {
        method: "POST",
        body: JSON.stringify({
          direction: document.querySelector("#direction").value,
          amount: document.querySelector("#amount").value,
          purpose: document.querySelector("#purpose").value
        })
      });
      event.currentTarget.reset();
      document.querySelector("#amount").focus();
      showNotice("Entry saved to your private ledger.");
      await refresh();
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      submit.disabled = false;
    }
  });

  document.querySelector("#budget-form").addEventListener("submit", async event => {
    event.preventDefault();
    try {
      await requestJson("/api/budget", {
        method: "POST",
        body: JSON.stringify({ amount: document.querySelector("#budget-amount").value })
      });
      showNotice("Monthly budget saved.");
      await refresh();
    } catch (error) {
      showNotice(error.message, true);
    }
  });

  let resizeFrame;
  window.addEventListener("resize", () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => { if (summary) render(summary); });
  });
  refresh();
}
