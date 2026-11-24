// The browser boundary every Playwright suite of this project runs inside (decision 0028).
// Both playwright.config.ts (hub e2e) and playwright.walk.config.ts (C10-WALK) spread
// `boundaryUse` and declare `boundaryProjects()`, so the two suites cannot drift apart.
// `boundary-checks.ts` proves on every run that these options are in effect. This file only holds
// constants and the project list (no `test` import) so that a config file may import it.
//
// What it is: the engine is Playwright's own Chromium headless shell (no `channel`, so never an
// installed Edge or Chrome that integrates with the Windows account), launched with Playwright's
// default fresh temporary profile (no persistent context, no userDataDir). The browser's own
// traffic is confined to loopback: everything that is not loopback is sent to a closed local port.
//
// Do NOT replace the raw proxy args below with Playwright's `proxy` launch option: Playwright
// appends `<-loopback>` to the bypass list for that option unless the list names a loopback host,
// which would send loopback (the hub) through the dead proxy as well.
import type { PlaywrightTestConfig } from "@playwright/test";

/** Exact version of the headless shell pinned by @playwright/test 1.63.0 (r1243, browsers.json). */
export const EXPECTED_BROWSER_VERSION = "153.0.8010.12";

/** The dead end: nothing listens on this port (TCP "discard"), so every proxied request fails. */
export const DEAD_PROXY = "http://127.0.0.1:9";

/**
 * C21-WALK (decision 0091): the walk's hub is served at its home's own host name,
 * `h<16 hex>.localhost`, which the resolver rule below would not resolve. The walk harness names
 * exactly that one host in PROMETHEOS_BOUNDARY_HOME_HOST; it is admitted by name and nothing
 * else is (the controls still prove that another `.localhost` name is not resolved). Unset in
 * every other suite, where the arguments are unchanged.
 */
function homeHostRule(): string {
  const host = process.env.PROMETHEOS_BOUNDARY_HOME_HOST;
  if (!host) return "";
  if (!/^h[0-9a-f]{16}\.localhost$/.test(host)) {
    throw new Error(`PROMETHEOS_BOUNDARY_HOME_HOST is not a home host name: ${host}`);
  }
  return `, EXCLUDE ${host}`;
}

export const BOUNDARY_ARGS: readonly string[] = [
  // Every HTTP, HTTPS and WebSocket request that is not loopback goes to the dead proxy and
  // fails with ERR_PROXY_CONNECTION_FAILED; loopback bypasses the proxy and goes direct.
  `--proxy-server=${DEAD_PROXY}`,
  "--proxy-bypass-list=localhost;127.0.0.1;[::1]",
  // No DNS for non-local names. This rule is also the only thing that stops the destinations
  // Chromium never sends to a proxy (it bypasses it implicitly for 127.0.0.0/8, [::1], *.localhost
  // and link-local addresses). IP literals are matched by these rules too, so the loopback
  // addresses the hub uses must be excluded explicitly (without them 127.0.0.1 cannot load).
  `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1, EXCLUDE ::1${homeHostRule()}`,
  // WebRTC may only use UDP through a proxy, and there is none: no peer-to-peer or STUN traffic.
  "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
  // QUIC (HTTP/3 over UDP) does not go through an HTTP proxy.
  "--disable-quic",
];

export const boundaryUse: NonNullable<PlaywrightTestConfig["use"]> = {
  browserName: "chromium",
  headless: true,
  launchOptions: { args: [...BOUNDARY_ARGS] },
};

/**
 * The boundary controls are a Playwright DEPENDENCY project of the journeys: they run before
 * them under any file filter or --grep, and when a control fails the journeys do not run at all.
 * Neither project overrides `use`, so both launch the browser with `boundaryUse`.
 */
export function boundaryProjects(): NonNullable<PlaywrightTestConfig["projects"]> {
  const controls = /00-boundary\.spec\.ts$/;
  return [
    { name: "boundary", testMatch: controls },
    { name: "chromium", testIgnore: controls, dependencies: ["boundary"] },
  ];
}
