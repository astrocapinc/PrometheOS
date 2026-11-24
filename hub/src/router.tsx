// A tiny History-API router: the hub serves index.html for every non-API path, so plain
// paths work on reload and with the browser's back/forward buttons.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from "react";

interface RouterValue {
  path: string;
  search: string;
  hash: string;
  navigate: (to: string, options?: { replace?: boolean }) => void;
}

const RouterContext = createContext<RouterValue>({
  path: "/",
  search: "",
  hash: "",
  navigate: () => undefined,
});

export function RouterProvider({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState(() => ({
    path: window.location.pathname,
    search: window.location.search,
    hash: window.location.hash,
  }));

  useEffect(() => {
    const onPop = () =>
      setLocation({
        path: window.location.pathname,
        search: window.location.search,
        hash: window.location.hash,
      });
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: string, options?: { replace?: boolean }) => {
    const url = new URL(to, window.location.origin);
    const target = url.pathname + url.search + url.hash;
    if (target === window.location.pathname + window.location.search + window.location.hash) return;
    if (options?.replace) window.history.replaceState(null, "", target);
    else window.history.pushState(null, "", target);
    setLocation({ path: url.pathname, search: url.search, hash: url.hash });
  }, []);

  return (
    <RouterContext.Provider value={{ ...location, navigate }}>{children}</RouterContext.Provider>
  );
}

export function useRouter(): RouterValue {
  return useContext(RouterContext);
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { to: string };

export function Link({ to, onClick, children, ...rest }: LinkProps) {
  const { navigate } = useRouter();
  const handle = (event: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(event);
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    navigate(to);
  };
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  );
}

/** Match "/meetings/:id" style patterns; returns params or null. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const a = pattern.split("/").filter(Boolean);
  const b = path.split("/").filter(Boolean);
  if (a.length !== b.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < a.length; i += 1) {
    const p = a[i] ?? "";
    const v = b[i] ?? "";
    if (p.startsWith(":")) params[p.slice(1)] = decodeURIComponent(v);
    else if (p !== v) return null;
  }
  return params;
}

/** After data renders, bring the element named by the URL hash into view and focus it. */
export function useHashTarget(ready: boolean): void {
  const { hash } = useRouter();
  useEffect(() => {
    if (!ready || !hash) return;
    const el = document.getElementById(decodeURIComponent(hash.slice(1)));
    if (!el) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" });
    if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "-1");
    el.focus({ preventScroll: true });
  }, [ready, hash]);
}
