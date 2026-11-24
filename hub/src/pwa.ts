// The installable app (A07, CP21.3; decision 0090). The hub serves `/sw.js` itself, so its cache
// is named by the build digest; it keeps only the built page and its assets, never a response
// from /api, and fetches pages network-first. Registration waits for the page to load so it
// never competes with the launch-code exchange.

export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {
      // The hub works without it; only installing it as an app needs it.
    });
  });
}
