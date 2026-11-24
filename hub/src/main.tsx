import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { HubProvider } from "./hub";
import { takeLaunchCode } from "./launch";
import { registerServiceWorker } from "./pwa";
import { RouterProvider } from "./router";
import { initTheme } from "./theme";
import "./styles.css";

takeLaunchCode(); // before the router reads the location and before any request
initTheme();
registerServiceWorker();

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <RouterProvider>
        <HubProvider>
          <App />
        </HubProvider>
      </RouterProvider>
    </StrictMode>,
  );
}
