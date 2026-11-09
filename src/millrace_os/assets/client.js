"use strict";
// The package-owned frontend launcher. It reads only the frontend selection
// and never loads code from a selected bundle.
(async () => {
  const nonce = location.hash.slice(1);
  // The one-use nonce leaves the address bar before any request.
  history.replaceState(null, "", location.pathname);
  const status = document.getElementById("status");
  const same = {
    mode: "same-origin", credentials: "same-origin", redirect: "error", cache: "no-store",
  };
  const reasons = {
    frontend_unavailable: "its files are missing or unreadable",
    frontend_incompatible: "it does not support this service's API contracts or capabilities",
    frontend_conflict: "its files changed after it was selected; validate and select it again",
    unsafe_path: "its directory is not safely owned (a symbolic link, or a folder other users can change)",
    frontend_selection_invalid: "the saved selection file is unreadable",
  };
  // The stock shell is public: its availability needs no sign-in.
  fetch("/ui/stock/", same).then(async (stock) => {
    if (stock.ok) return;
    const code = await stock.json().then((error) => error.code, () => "unavailable");
    const notice = document.getElementById("stock-status");
    notice.textContent = `The stock frontend is not available (${code}). Recovery still works.`;
    notice.hidden = false;
  }, () => {});
  try {
    if (nonce) {
      const exchanged = await fetch("/api/v1/auth/exchange", {
        ...same,
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({nonce}),
      });
      // The launcher only reads, so it keeps no CSRF value.
      if (!exchanged.ok) {
        status.textContent =
          "This sign-in link has expired or was already used. Run millrace-os open --client again.";
        return;
      }
    }
    const reply = await fetch("/api/v1/client/frontend", same);
    if (reply.status === 401) return; // keep the sign-in hint
    if (!reply.ok) {
      status.textContent = `The frontend selection could not be read (HTTP ${reply.status}).`;
      return;
    }
    const state = await reply.json();
    const known = (choice) =>
      state.manifest !== null && state.manifest.bundle_sha256 === choice.bundle_sha256;
    const name = (choice) => {
      if (choice === null) return "Unreadable saved selection";
      if (choice.kind === "stock") return "Stock frontend";
      const label = known(choice)
        ? `${state.manifest.frontend_id} ${state.manifest.frontend_version}`
        : "Local frontend";
      return `${label}, bundle ${choice.bundle_sha256}, from ${choice.path}`;
    };
    document.getElementById("desired").textContent = name(state.desired);
    document.getElementById("effective").textContent =
      state.effective.kind === "stock" ? "Stock frontend" : name(state.desired);
    document.getElementById("version").textContent = state.version ?? "none saved";
    status.textContent = state.reason === null
      ? "The selected frontend is in use."
      : `The stock frontend is in use because the selected frontend is not available: ${
        reasons[state.reason] ?? state.reason}.`;
    const base = state.effective.base;
    const link = document.getElementById("open-link");
    link.href = /^\/ui\/(stock|local\/[0-9a-f]{64})\/$/.test(base) ? base : "/ui/stock/";
    document.getElementById("selection").hidden = false;
    document.getElementById("open").hidden = false;
  } catch (_) {
    status.textContent = "The service could not be reached.";
  }
})();
