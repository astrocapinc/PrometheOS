// Theme: dark by default (owner's choice, 2026-10-18), light or "follow Windows" on request.
// The choice is written to localStorage only when the owner makes one, so a fresh browser
// stores nothing new. The stylesheet reads <html data-theme>: no attribute means dark.
import { useEffect, useState } from "react";

export type ThemeChoice = "dark" | "light" | "system";

const KEY = "prometheos.hub.theme";
const CHOICES: ThemeChoice[] = ["dark", "light", "system"];
const listeners = new Set<(choice: ThemeChoice) => void>();

export const THEME_LABELS: Record<ThemeChoice, string> = {
  dark: "Dark",
  light: "Light",
  system: "Follow Windows",
};

function readChoice(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored && (CHOICES as string[]).includes(stored)) return stored as ThemeChoice;
  } catch {
    // Storage may be unavailable; the default applies.
  }
  return "dark";
}

// The page background of each theme (styles.css --bg). The desktop window reads it from
// <meta name="theme-color"> to paint before the page loads and to set its title bar. "Follow
// Windows" uses the same colours one step off, so every change of choice changes the colour
// (the window is only told about changes) and the window can tell that mode apart.
const THEME_COLOR = { dark: "#0e1116", light: "#f6f7f9" } as const;
const SYSTEM_THEME_COLOR = { dark: "#0e1117", light: "#f6f7f8" } as const;
const lightQuery = () => window.matchMedia("(prefers-color-scheme: light)");

function syncThemeColor(choice: ThemeChoice) {
  const resolved = choice === "system" ? (lightQuery().matches ? "light" : "dark") : choice;
  let meta = document.querySelector<HTMLMetaElement>('meta[name="theme-color"]');
  if (!meta) {
    meta = document.createElement("meta");
    meta.name = "theme-color";
    document.head.appendChild(meta);
  }
  meta.content = choice === "system" ? SYSTEM_THEME_COLOR[resolved] : THEME_COLOR[resolved];
}

function apply(choice: ThemeChoice) {
  const root = document.documentElement;
  if (choice === "dark") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
  syncThemeColor(choice);
}

/** The desktop window passes the owner's remembered choice as ?theme= (its hub origin, and so
 *  this page's storage, changes with each start). Taken once, then removed from the address. */
function takeThemeParam(): ThemeChoice | null {
  const url = new URL(window.location.href);
  const theme = url.searchParams.get("theme");
  if (theme === null) return null;
  url.searchParams.delete("theme");
  window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  if (!(CHOICES as string[]).includes(theme)) return null;
  try {
    window.localStorage.setItem(KEY, theme);
  } catch {
    // the choice still applies to this page
  }
  return theme as ThemeChoice;
}

/** Apply the stored choice before the first render (called from main.tsx). */
export function initTheme() {
  apply(takeThemeParam() ?? readChoice());
  // "Follow Windows" follows a change made while the hub is open.
  lightQuery().addEventListener("change", () => {
    const choice = readChoice();
    if (choice === "system") syncThemeColor(choice);
  });
}

export function setTheme(choice: ThemeChoice) {
  try {
    window.localStorage.setItem(KEY, choice);
  } catch {
    // The theme still changes for this page.
  }
  apply(choice);
  listeners.forEach((listener) => listener(choice));
}

export function nextTheme(choice: ThemeChoice): ThemeChoice {
  return CHOICES[(CHOICES.indexOf(choice) + 1) % CHOICES.length] ?? "dark";
}

export function useTheme(): [ThemeChoice, (choice: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(readChoice);
  useEffect(() => {
    listeners.add(setChoice);
    return () => {
      listeners.delete(setChoice);
    };
  }, []);
  return [choice, setTheme];
}
