function doPost(event) {
  try {
    const envelope = JSON.parse(event.postData.contents);
    const secret =
      PropertiesService.getScriptProperties().getProperty("MAILER_SECRET");
    if (!secret || !envelope.payload || !envelope.signature) {
      throw new Error("Unauthorized request");
    }

    const digest = Utilities.computeHmacSha256Signature(
      envelope.payload,
      secret,
    );
    const expected = digest
      .map((byte) => ("0" + (byte & 255).toString(16)).slice(-2))
      .join("");
    if (expected !== envelope.signature) {
      throw new Error("Invalid request signature");
    }

    const message = JSON.parse(envelope.payload);
    const sentAt = Number(message.timestamp) * 1000;
    if (
      !Number.isFinite(sentAt) ||
      Math.abs(Date.now() - sentAt) > 5 * 60 * 1000
    ) {
      throw new Error("Request expired");
    }
    if (
      !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(message.to) ||
      !/^\d{1,6}$/.test(message.code)
    ) {
      throw new Error("Invalid email or code");
    }

    MailApp.sendEmail(
      message.to,
      "Your Moneyline verification code",
      "Your account verification code is " +
        message.code +
        ". It expires in 15 minutes.",
    );
    return jsonResponse({ ok: true });
  } catch (error) {
    console.error(error);
    return jsonResponse({ ok: false, error: "delivery_failed" });
  }
}

function jsonResponse(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(
    ContentService.MimeType.JSON,
  );
}
