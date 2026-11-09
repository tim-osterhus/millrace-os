// Entry. Trust starts before anything else: `establishTrust` consumes and
// removes the fragment nonce synchronously, before the first render and
// before any other request. The app mounts with every read and write gated on
// that trust.

import "./styles.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App.tsx";
import { establishTrust } from "./session.ts";

const root = document.getElementById("root");
if (root !== null) {
  void establishTrust();
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
