"use strict";
(async () => {
  let nonce = location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const recovery = location.pathname === "/recovery";
  const status = document.getElementById("status");
  if (!nonce) return;
  try {
    const response = await fetch("/api/v1/auth/exchange", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({nonce, recovery})
    });
    nonce = "";
    if (!response.ok) throw new Error("authentication");
    // This lexical variable is the only CSRF store. No persistent browser storage.
    const {csrf} = await response.json();
    const identity = await fetch("/api/v1/identity", {credentials: "same-origin"});
    if (!identity.ok || !csrf) throw new Error("authentication");
    status.textContent = "Authenticated local operator";
    if (recovery) {
      const diagnostics = await fetch("/api/v1/recovery/diagnostics", {credentials: "same-origin"});
      if (!diagnostics.ok) throw new Error("diagnostics");
      document.getElementById("diagnostics").textContent = JSON.stringify((await diagnostics.json()).data, null, 2);
    }
  } catch (_) {
    nonce = "";
    status.textContent = "Authentication unavailable. Run millrace-os open again.";
  }
})();
