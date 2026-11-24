// The launcher's one-use code (A07, CP21; decision 0088). `prometheos start` opens the hub
// with it in the URL fragment only, which never reaches a request line, Referer or server log.
// It is taken before the app renders or sends any request and removed from the current history
// entry at once; the session check then exchanges it by a same-origin POST.

let launchCode: string | null = null;

const FRAGMENT = /^#pair=([A-Za-z0-9_-]{16,200})$/;

export function takeLaunchCode(): void {
  const match = FRAGMENT.exec(window.location.hash);
  if (!match) return;
  launchCode = match[1] ?? null;
  window.history.replaceState(
    window.history.state,
    "",
    window.location.pathname + window.location.search,
  );
}

export function consumeLaunchCode(): string | null {
  const code = launchCode;
  launchCode = null;
  return code;
}
