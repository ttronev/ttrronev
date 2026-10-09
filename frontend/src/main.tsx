import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router";
import App from "@/App";
import { applySystemTheme } from "@/lib/theme";
import "@/index.css";

applySystemTheme();

// Vite's base ("/app/") is also the router basename, so FastAPI's /app mount
// and the dev server agree on every URL.
const basename = import.meta.env.BASE_URL.replace(/\/$/, "");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter basename={basename}>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
