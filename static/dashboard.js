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
    const response = await fetch(url, {
      ...options,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken, ...options.headers }
    });
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
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);
    const total = items.reduce((sum, item) => sum + item.total, 0);
    empty.hidden = total > 0;
    if (!total) return;

    const centerX = width / 2;
    const centerY = height / 2;
    const radius = Math.min(width, height) * 0.39;
    const lineWidth = Math.max(16, radius * 0.27);
    let angle = -Math.PI / 2;
    items.slice(0, 8).forEach((item, index) => {
      const slice = item.total / total * Math.PI * 2;
      context.beginPath();
      context.arc(centerX, centerY, radius, angle, angle + slice - 0.025);
      context.strokeStyle = colors[index % colors.length];
      context.lineWidth = lineWidth;
      context.lineCap = "butt";
      context.stroke();
      angle += slice;
      const legendRow = document.createElement("li");
      const swatch = document.createElement("i");
      const label = document.createElement("span");
      const value = document.createElement("strong");
      swatch.style.backgroundColor = colors[index % colors.length];
      label.textContent = item.purpose;
      value.textContent = money(item.total);
      legendRow.append(swatch, label, value);
      legend.append(legendRow);
    });
    context.fillStyle = "#e5ecd7";
    context.font = "700 13px 'Manrope', sans-serif";
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText("TOTAL USED", centerX, centerY - 9);
    context.font = "500 15px 'IBM Plex Mono', monospace";
    context.fillText(money(total), centerX, centerY + 13);
    if (items.length > 8) {
      const extra = items.slice(8).reduce((sum, item) => sum + item.total, 0);
      const other = document.createElement("li");
      const swatch = document.createElement("i");
      const label = document.createElement("span");
      const value = document.createElement("strong");
      swatch.style.backgroundColor = colors[8 % colors.length];
      label.textContent = "Other purposes";
      value.textContent = money(extra);
      other.append(swatch, label, value);
      legend.append(other);
    }
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
    context.scale(ratio, ratio);
    context.clearRect(0, 0, width, height);

    const monthMap = new Map(months.map(item => [`${item.month}:${item.direction}`, item.total]));
    const current = new Date();
    const series = Array.from({ length: 6 }, (_, index) => {
      const date = new Date(current.getFullYear(), current.getMonth() - 5 + index, 1);
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
      return { key, label: date.toLocaleString(undefined, { month: "short" }), received: monthMap.get(`${key}:received`) || 0, used: monthMap.get(`${key}:used`) || 0 };
    });
    const max = Math.max(1, ...series.flatMap(item => [item.received, item.used]));
    empty.hidden = series.every(item => !item.received && !item.used);
    const top = 12;
    const bottom = height - 26;
    const plotHeight = bottom - top;
    const groupWidth = width / series.length;
    const barWidth = Math.min(16, groupWidth * 0.24);

    context.strokeStyle = "#35443a";
    context.lineWidth = 1;
    for (let line = 0; line < 4; line += 1) {
      const y = top + plotHeight * line / 3;
      context.beginPath();
      context.moveTo(0, y);
      context.lineTo(width, y);
      context.stroke();
    }
    series.forEach((item, index) => {
      const center = groupWidth * (index + 0.5);
      const receivedHeight = item.received / max * plotHeight;
      const usedHeight = item.used / max * plotHeight;
      context.fillStyle = colors[0];
      context.fillRect(center - barWidth - 2, bottom - receivedHeight, barWidth, receivedHeight);
      context.fillStyle = colors[1];
      context.fillRect(center + 2, bottom - usedHeight, barWidth, usedHeight);
      context.fillStyle = "#a3af9a";
      context.font = "11px 'Manrope', sans-serif";
      context.textAlign = "center";
      context.fillText(item.label, center, height - 7);
    });
  }

  function drawTransactions(transactions) {
    rows.replaceChildren();
    if (!transactions.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.className = "empty-row";
      cell.textContent = "Your first entry will show up here.";
      row.append(cell);
      rows.append(row);
      return;
    }
    transactions.forEach(transaction => {
      const row = document.createElement("tr");
      const type = document.createElement("td");
      const purpose = document.createElement("td");
      const date = document.createElement("td");
      const amount = document.createElement("td");
      type.innerHTML = transaction.direction === "received" ? '<span class="transaction-type type-received">Received</span>' : '<span class="transaction-type type-used">Used</span>';
      purpose.textContent = transaction.purpose;
      date.textContent = new Date(transaction.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
      amount.className = `amount-col ${transaction.direction === "received" ? "money-positive" : "money-negative"}`;
      amount.textContent = `${transaction.direction === "received" ? "+" : "−"}${money(transaction.amount_cents)}`;
      row.append(type, purpose, date, amount);
      rows.append(row);
    });
  }

  function render(data) {
    summary = data;
    document.querySelector("#total-received").textContent = money(data.received);
    document.querySelector("#total-spent").textContent = money(data.spent);
    document.querySelector("#total-balance").textContent = money(data.balance);
    const budgetMessage = document.querySelector("#budget-message");
    const progress = document.querySelector("#budget-progress");
    const budgetInput = document.querySelector("#budget-amount");
    if (data.monthly_budget) {
      const percent = Math.round(data.monthly_spent / data.monthly_budget * 100);
      budgetMessage.textContent = `${money(data.monthly_spent)} of ${money(data.monthly_budget)} used this month${percent > 100 ? " · Budget exceeded" : ` · ${percent}%`}`;
      progress.style.width = `${Math.min(percent, 100)}%`;
      progress.classList.toggle("is-over", percent >= 90);
      budgetInput.value = (data.monthly_budget / 100).toFixed(2);
    } else {
      budgetMessage.textContent = "Set a monthly budget to get started.";
      progress.style.width = "0%";
    }
    drawDonut(data.purposes);
    drawTrend(data.months);
    drawTransactions(data.transactions);
  }

  async function refresh() {
    try {
      render(await requestJson("/api/summary", { headers: {} }));
    } catch (error) {
      showNotice(error.message, true);
    }
  }

  function showNotice(message, isError = false) {
    let notice = document.querySelector("#form-notice");
    if (!notice) {
      notice = document.createElement("p");
      notice.id = "form-notice";
      notice.className = "form-note form-response";
      document.querySelector("#transaction-form").append(notice);
    }
    notice.textContent = message;
    notice.classList.toggle("is-error", isError);
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
        body: JSON.stringify({ direction: document.querySelector("#direction").value, amount: document.querySelector("#amount").value, purpose: document.querySelector("#purpose").value })
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
      await requestJson("/api/budget", { method: "POST", body: JSON.stringify({ amount: document.querySelector("#budget-amount").value }) });
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