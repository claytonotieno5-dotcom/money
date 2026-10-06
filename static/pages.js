const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

async function requestJson(url, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-CSRF-Token", csrfToken);
  if (!(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(url, { ...options, headers });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("The server returned an unreadable response.");
  }
  if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
  return data;
}

const formatMoney = cents => new Intl.NumberFormat("en-KE", {
  style: "currency", currency: "KES", minimumFractionDigits: 2
}).format(cents / 100);

const transactionRows = document.querySelector("#page-transaction-rows");
if (transactionRows) {
  const status = document.querySelector("#transaction-status");
  let direction = "received";

  document.querySelectorAll(".segment").forEach(button => {
    button.addEventListener("click", () => {
      direction = button.dataset.direction;
      document.querySelectorAll(".segment").forEach(item => {
        const selected = item === button;
        item.classList.toggle("is-selected", selected);
        item.setAttribute("aria-pressed", String(selected));
      });
    });
  });

  function renderTransactions(items) {
    transactionRows.replaceChildren();
    document.querySelector("#transaction-count").textContent = `${items.length} most recent`;
    if (!items.length) {
      transactionRows.innerHTML = '<tr><td colspan="5" class="empty-row">No transactions yet.</td></tr>';
      return;
    }
    items.forEach(item => {
      const row = document.createElement("tr");
      const typeCell = document.createElement("td");
      const type = document.createElement("span");
      type.className = `transaction-type ${item.direction === "received" ? "type-received" : "type-used"}`;
      type.textContent = item.direction === "received" ? "Received" : "Used";
      typeCell.append(type);
      const purpose = document.createElement("td");
      purpose.textContent = item.purpose;
      const date = document.createElement("td");
      date.textContent = new Date(item.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
      const amount = document.createElement("td");
      amount.className = `amount-col ${item.direction === "received" ? "money-positive" : "money-negative"}`;
      amount.textContent = `${item.direction === "received" ? "+" : "−"}${formatMoney(item.amount_cents)}`;
      const action = document.createElement("td");
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "delete-transaction";
      remove.textContent = "Delete";
      remove.addEventListener("click", async () => {
        if (!window.confirm("Delete this transaction? This cannot be undone.")) return;
        remove.disabled = true;
        try {
          await requestJson(`/api/transactions/${item.id}`, { method: "DELETE" });
          status.textContent = "Transaction deleted.";
          status.classList.remove("is-error");
          await loadTransactions();
        } catch (error) {
          status.textContent = error.message;
          status.classList.add("is-error");
          remove.disabled = false;
        }
      });
      action.append(remove);
      row.append(typeCell, purpose, date, amount, action);
      transactionRows.append(row);
    });
  }

  async function loadTransactions() {
    try {
      renderTransactions((await requestJson("/api/summary", { headers: {} })).transactions);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("is-error");
    }
  }

  document.querySelector("#page-transaction-form").addEventListener("submit", async event => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button[type='submit']");
    submit.disabled = true;
    try {
      await requestJson("/api/transactions", {
        method: "POST",
        body: JSON.stringify({
          direction,
          amount: document.querySelector("#page-amount").value,
          purpose: document.querySelector("#page-purpose").value
        })
      });
      event.currentTarget.reset();
      status.textContent = "Transaction added.";
      status.classList.remove("is-error");
      await loadTransactions();
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("is-error");
    } finally {
      submit.disabled = false;
    }
  });
  loadTransactions();
}

const importFile = document.querySelector("#import-file");
if (importFile) {
  const status = document.querySelector("#import-status");
  const preview = document.querySelector("#import-preview");
  const rows = document.querySelector("#import-rows");
  const approve = document.querySelector("#approve-import");
  const video = document.querySelector("#camera-video");
  const cameraView = document.querySelector("#camera-view");
  const cameraStart = document.querySelector("#camera-start");
  const cameraCapture = document.querySelector("#camera-capture");
  const cameraStop = document.querySelector("#camera-stop");
  const cameraStatus = document.querySelector("#camera-status");
  let cameraStream;

  async function loadOcr() {
    if (window.Tesseract) return;
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/tesseract.js@6.0.1/dist/tesseract.min.js";
      script.onload = resolve;
      script.onerror = () => reject(new Error("Could not load the on-device OCR library. Check your internet connection."));
      document.head.append(script);
    });
  }

  function displayPreview(transactions, skipped = 0) {
    rows.replaceChildren();
    const dateWarnings = transactions.filter(item => item.date_warning).length;
    transactions.forEach(item => {
      const row = document.createElement("tr");
      const includeCell = document.createElement("td");
      const include = document.createElement("input");
      include.type = "checkbox";
      include.checked = true;
      include.setAttribute("aria-label", "Include transaction");
      includeCell.append(include);

      const typeCell = document.createElement("td");
      const type = document.createElement("select");
      [["received", "Received"], ["used", "Used"]].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        type.append(option);
      });
      type.value = item.direction;
      typeCell.append(type);

      const amountCell = document.createElement("td");
      const amount = document.createElement("input");
      amount.type = "number";
      amount.min = "0.01";
      amount.step = "0.01";
      amount.required = true;
      amount.value = item.amount;
      amount.setAttribute("aria-label", "Amount in Kenyan shillings");
      amountCell.append(amount);

      const purposeCell = document.createElement("td");
      const purpose = document.createElement("input");
      purpose.type = "text";
      purpose.maxLength = 120;
      purpose.required = true;
      purpose.value = item.purpose;
      purpose.setAttribute("aria-label", "Purpose");
      purposeCell.append(purpose);

      const dateCell = document.createElement("td");
      const date = document.createElement("input");
      date.type = "date";
      date.value = item.date ? item.date.slice(0, 10) : "";
      if (item.date_warning) date.title = "Correct this date or leave blank to use approval time.";
      dateCell.append(date);
      row.append(includeCell, typeCell, amountCell, purposeCell, dateCell);
      rows.append(row);
    });
    preview.hidden = transactions.length === 0;
    approve.disabled = false;
    status.textContent = `${transactions.length} row${transactions.length === 1 ? "" : "s"} found. Review every row before approving.${skipped ? ` ${skipped} unreadable row${skipped === 1 ? " was" : "s were"} skipped.` : ""}${dateWarnings ? ` ${dateWarnings} date${dateWarnings === 1 ? " could" : "s could"} not be read.` : ""}`;
    status.classList.remove("is-error");
  }

  async function scanImage(file) {
    if (file.size > 2 * 1024 * 1024) throw new Error("Images must be 2 MB or smaller.");
    status.textContent = "Loading on-device OCR…";
    await loadOcr();
    const worker = await window.Tesseract.createWorker("eng", 1, {
      logger: progress => {
        if (progress.status === "recognizing text") {
          status.textContent = `Reading on this device… ${Math.round(progress.progress * 100)}%`;
        }
      }
    });
    let text;
    try {
      ({ data: { text } } = await worker.recognize(file));
    } finally {
      await worker.terminate();
    }
    const result = await requestJson("/api/import/preview", {
      method: "POST",
      body: JSON.stringify({ text })
    });
    displayPreview(result.transactions, result.skipped);
  }

  async function processFile(file) {
    if (!file) return;
    preview.hidden = true;
    status.classList.remove("is-error");
    try {
      if (file.size > 2 * 1024 * 1024) throw new Error("Files must be 2 MB or smaller.");
      if (file.type === "text/csv" || file.name.toLowerCase().endsWith(".csv")) {
        const data = new FormData();
        data.append("file", file);
        status.textContent = "Reading CSV…";
        const result = await requestJson("/api/import/preview", { method: "POST", body: data });
        displayPreview(result.transactions, result.skipped);
      } else if (file.type.startsWith("image/")) {
        await scanImage(file);
      } else {
        throw new Error("Choose a CSV or image file.");
      }
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("is-error");
    }
  }

  importFile.addEventListener("change", () => processFile(importFile.files[0]));
  cameraStart.addEventListener("click", async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      cameraStatus.textContent = "Camera access is unavailable. Use localhost or HTTPS and a supported browser, or select a photo file.";
      cameraStatus.classList.add("is-error");
      return;
    }
    try {
      cameraStream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 } }
      });
      video.srcObject = cameraStream;
      await video.play();
      cameraView.hidden = false;
      cameraCapture.hidden = false;
      cameraStop.hidden = false;
      cameraStart.hidden = true;
      cameraStatus.textContent = "Camera ready. Hold the statement steady and tap Take photo.";
      cameraStatus.classList.remove("is-error");
    } catch (error) {
      cameraStatus.textContent = error.name === "NotAllowedError"
        ? "Camera permission was denied. Allow camera access in your browser settings, then try again."
        : `Could not open the camera (${error.name || "device error"}). Use a photo file instead.`;
      cameraStatus.classList.add("is-error");
    }
  });

  function stopCamera() {
    cameraStream?.getTracks().forEach(track => track.stop());
    cameraStream = null;
    video.srcObject = null;
    cameraView.hidden = true;
    cameraCapture.hidden = true;
    cameraStop.hidden = true;
    cameraStart.hidden = false;
  }
  cameraStop.addEventListener("click", stopCamera);
  cameraCapture.addEventListener("click", () => {
    const canvas = document.querySelector("#camera-canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    canvas.toBlob(async blob => {
      if (!blob) {
        cameraStatus.textContent = "The camera could not capture an image. Try again or select a photo.";
        cameraStatus.classList.add("is-error");
        return;
      }
      stopCamera();
      cameraStatus.textContent = "Photo captured. Scanning on this device…";
      await processFile(new File([blob], "camera-statement.jpg", { type: "image/jpeg" }));
    }, "image/jpeg", 0.9);
  });

  approve.addEventListener("click", async () => {
    const transactions = [...rows.querySelectorAll("tr")].flatMap(row => {
      if (!row.querySelector('input[type="checkbox"]').checked) return [];
      const controls = row.querySelectorAll("select, input");
      return [{
        direction: controls[0].value,
        amount: controls[1].value,
        purpose: controls[2].value,
        date: controls[3].value
      }];
    });
    approve.disabled = true;
    try {
      const result = await requestJson("/api/transactions/import", {
        method: "POST",
        body: JSON.stringify({ transactions })
      });
      preview.hidden = true;
      importFile.value = "";
      status.textContent = `${result.count} reviewed transaction${result.count === 1 ? "" : "s"} added.`;
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("is-error");
      approve.disabled = false;
    }
  });
}

const profileInput = document.querySelector("#profile-picture");
if (profileInput) {
  profileInput.addEventListener("change", () => {
    const file = profileInput.files[0];
    if (!file) return;
    const image = document.querySelector("#profile-preview");
    const placeholder = document.querySelector("#profile-placeholder");
    image.src = URL.createObjectURL(file);
    image.hidden = false;
    if (placeholder) placeholder.hidden = true;
  });
}

const voiceButton = document.querySelector("#voice-start");
if (voiceButton) {
  const status = document.querySelector("#voice-status");
  const stopButton = document.querySelector("#voice-stop");
  const recognitionConstructor = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition;
  let isListening = false;

  function stopRecognition() {
    if (isListening && recognition) recognition.stop();
  }
  stopButton.addEventListener("click", stopRecognition);

  voiceButton.addEventListener("click", async () => {
    if (!recognitionConstructor) {
      status.textContent = "This browser does not provide speech-to-text. Open Moneyline in the latest Chrome or Edge and allow microphone access, or type the transaction.";
      status.classList.add("is-error");
      return;
    }
    const localOnly = document.querySelector("#voice-mode").value === "local";
    if (!localOnly && !document.querySelector("#voice-service-consent").checked) {
      status.textContent = "Tick the audio-processing consent box before using the browser speech service.";
      status.classList.add("is-error");
      return;
    }
    let permissionStream;
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("Microphone access requires localhost or HTTPS in a supported browser.");
      }
      permissionStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      permissionStream.getTracks().forEach(track => track.stop());
    } catch (error) {
      permissionStream?.getTracks().forEach(track => track.stop());
      status.textContent = error.name === "NotAllowedError"
        ? "Microphone permission was denied. Allow microphone access for this site in browser settings, then try again."
        : error.name === "NotFoundError"
          ? "No microphone was found. Connect or enable a microphone and try again."
          : error.message || `Could not access microphone (${error.name || "device error"}).`;
      status.classList.add("is-error");
      return;
    }

    recognition = new recognitionConstructor();
    recognition.lang = "en-US";
    recognition.continuous = false;
    recognition.interimResults = true;
    if (localOnly) {
      if (!("processLocally" in recognition)) {
        status.textContent = "On-device recognition is unavailable in this browser. Select Browser speech service and confirm its audio privacy notice.";
        status.classList.add("is-error");
        return;
      }
      recognition.processLocally = true;
    }
    recognition.onstart = () => {
      isListening = true;
      voiceButton.disabled = true;
      stopButton.disabled = false;
      status.textContent = "Listening… speak one transaction, then tap Stop or wait for the pause.";
      status.classList.remove("is-error");
    };
    recognition.onresult = event => {
      const transcript = [...event.results].map(result => result[0].transcript).join(" ");
      document.querySelector("#voice-transcript").value = transcript.slice(0, 300);
    };
    recognition.onerror = event => {
      const messages = {
        "not-allowed": "Microphone or speech permission was denied. Allow it in browser settings.",
        "service-not-allowed": "The browser speech service is disabled. Check browser speech and privacy settings.",
        "audio-capture": "The browser could not use the microphone. Check that it is connected and not in use by another app.",
        network: "The browser speech service could not connect. Check your internet connection and try again.",
        "language-not-supported": "English speech recognition is not available in this mode. Select Browser speech service or type the entry."
      };
      status.textContent = messages[event.error] || `Speech recognition stopped (${event.error}). Check microphone permissions or type the entry.`;
      status.classList.add("is-error");
    };
    recognition.onend = () => {
      isListening = false;
      voiceButton.disabled = false;
      stopButton.disabled = true;
      if (!status.classList.contains("is-error")) {
        status.textContent = "Transcript ready. Review it, then analyze the transaction.";
      }
    };
    try {
      recognition.start();
    } catch (error) {
      status.textContent = `Could not start speech recognition (${error.name || "browser error"}). Check microphone settings and try again.`;
      status.classList.add("is-error");
      voiceButton.disabled = false;
      stopButton.disabled = true;
    }
  });

  document.querySelector("#voice-review").addEventListener("click", () => {
    const text = document.querySelector("#voice-transcript").value.trim();
    const pattern = /\b(received|income|earned|deposit(?:ed)?|spent|paid|used|bought|purchase|debit(?:ed)?|withdraw(?:al|n)?|expense)\s+(?:KES\s*)?([\d,]+(?:\.\d{1,2})?)\s*(?:KES\s*)?(?:(?:from|for|on|as|via)\s+)?([\w][\w .,&'-]{0,119})/i;
    const match = text.match(pattern);
    if (!match) {
      status.textContent = "I could not identify an amount and purpose. Try “received 1,500 from salary” or “spent 350 on transport”.";
      status.classList.add("is-error");
      return;
    }
    const amount = Number(match[2].replaceAll(",", ""));
    const purpose = match[3].trim().replace(/[.,;]+$/, "");
    if (!Number.isFinite(amount) || amount <= 0 || amount > 100_000_000 || !purpose) {
      status.textContent = "Check the amount and purpose, then try again.";
      status.classList.add("is-error");
      return;
    }
    document.querySelector("#voice-direction").value =
      ["received", "income", "earned", "deposit", "deposited"].includes(match[1].toLowerCase())
        ? "received"
        : "used";
    document.querySelector("#voice-amount").value = amount.toFixed(2);
    document.querySelector("#voice-purpose").value = purpose;
    document.querySelector("#voice-preview").hidden = false;
    status.textContent = "Review and correct the detected details below. Nothing has been saved yet.";
    status.classList.remove("is-error");
  });

  document.querySelector("#voice-approve").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await requestJson("/api/transactions", {
        method: "POST",
        body: JSON.stringify({
          direction: document.querySelector("#voice-direction").value,
          amount: document.querySelector("#voice-amount").value,
          purpose: document.querySelector("#voice-purpose").value
        })
      });
      document.querySelector("#voice-preview").hidden = true;
      document.querySelector("#voice-transcript").value = "";
      status.textContent = "Approved. Transaction saved to your ledger.";
      status.classList.remove("is-error");
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("is-error");
    } finally {
      button.disabled = false;
    }
  });

  document.querySelector("#voice-ask-coach").addEventListener("click", async event => {
    const question = document.querySelector("#voice-transcript").value.trim();
    if (!question) {
      status.textContent = "Speak or type a financial question first.";
      status.classList.add("is-error");
      return;
    }
    const button = event.currentTarget;
    const answer = document.querySelector("#coach-answer");
    button.disabled = true;
    answer.textContent = "Checking your recorded ledger…";
    answer.classList.remove("is-error");
    try {
      const result = await requestJson("/api/coach", {
        method: "POST",
        body: JSON.stringify({ question })
      });
      answer.textContent = result.answer;
    } catch (error) {
      answer.textContent = error.message;
      answer.classList.add("is-error");
    } finally {
      button.disabled = false;
    }
  });

  document.querySelector("#coach-form").addEventListener("submit", async event => {
    event.preventDefault();
    const answer = document.querySelector("#coach-answer");
    answer.textContent = "Checking your recorded ledger…";
    answer.classList.remove("is-error");
    try {
      const result = await requestJson("/api/coach", {
        method: "POST",
        body: JSON.stringify({ question: document.querySelector("#coach-question").value })
      });
      answer.textContent = result.answer;
    } catch (error) {
      answer.textContent = error.message;
      answer.classList.add("is-error");
    }
  });
}
