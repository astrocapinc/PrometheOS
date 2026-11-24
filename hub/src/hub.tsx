// Session, project snapshot, change feed and shared UI services for the whole app.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { ApiError, get, onConnectionEvent, post, q, setCsrf } from "./api";
import { consumeLaunchCode } from "./launch";
import { useRouter } from "./router";
import type { Changes, ProjectInfo, Session } from "./types";

export type PairStatus = "checking" | "unpaired" | "paired";

export interface SourceSpec {
  versionId: string;
  title: string;
  startLine?: number;
  endLine?: number;
  /** Audio citations (transcript spans) use milliseconds instead of lines. */
  startMs?: number;
  endMs?: number;
  /** A receipt citation in words (dogfood F5), used instead of a line range. */
  locatorLabel?: string;
  /** What cites this span, shown above the text. */
  citedBy?: string;
}

interface HubValue {
  status: PairStatus;
  /** A02, CP10.5: the hub serves only the recovery screen (the project store does not open). */
  maintenance: boolean;
  project: ProjectInfo | null;
  projectError: unknown;
  knownSeq: number;
  dataVersion: number;
  offline: boolean;
  lastChange: Changes | null;
  pair: (token: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<number>;
  announce: (message: string) => void;
  source: SourceSpec | null;
  openSource: (spec: SourceSpec, invoker?: HTMLElement | null) => void;
  closeSource: () => void;
}

const HubContext = createContext<HubValue | null>(null);

export function useHub(): HubValue {
  const value = useContext(HubContext);
  if (!value) throw new Error("useHub outside HubProvider");
  return value;
}

export function HubProvider({ children }: { children: ReactNode }) {
  const { path } = useRouter();
  const [status, setStatus] = useState<PairStatus>("checking");
  const [maintenance, setMaintenance] = useState(false);
  const [project, setProject] = useState<ProjectInfo | null>(null);
  const [projectError, setProjectError] = useState<unknown>(null);
  const [knownSeq, setKnownSeq] = useState(0);
  const [dataVersion, setDataVersion] = useState(0);
  const [offline, setOffline] = useState(false);
  const [lastChange, setLastChange] = useState<Changes | null>(null);
  const [message, setMessage] = useState("");
  const [source, setSource] = useState<SourceSpec | null>(null);
  const invokerRef = useRef<HTMLElement | null>(null);
  const seqRef = useRef(0);

  const announce = useCallback((text: string) => {
    // Clear then set so repeated identical messages are still announced.
    setMessage("");
    window.setTimeout(() => setMessage(text), 30);
  }, []);

  const loadProject = useCallback(async (): Promise<number> => {
    try {
      const info = await get<ProjectInfo>("/project");
      setProject(info);
      setProjectError(null);
      seqRef.current = info.project_seq;
      setKnownSeq(info.project_seq);
      return info.project_seq;
    } catch (err) {
      setProjectError(err);
      throw err;
    }
  }, []);

  // One session check at a time: requests failing together share it, so the CSRF token and
  // the session cookie always come from the same exchange (review cp21-a round 2).
  const sessionCheck = useRef<Promise<void> | null>(null);
  const checkSession = useCallback((): Promise<void> => {
    if (sessionCheck.current) return sessionCheck.current;
    const exchange = async (path: string, body: unknown): Promise<boolean> => {
      try {
        const reply = await post<{ paired: boolean; csrf_token: string }>(path, body, {
          quietUnpaired: true,
        });
        setCsrf(reply.csrf_token);
        setStatus("paired");
        return true;
      } catch {
        return false;
      }
    };
    const task = (async () => {
      try {
        const session = await get<Session>("/session", { quietUnpaired: true });
        setMaintenance(session.mode === "maintenance");
        if (session.paired && session.csrf_token) {
          setCsrf(session.csrf_token);
          setStatus("paired");
          return;
        }
        const launchCode = consumeLaunchCode();
        // A remembered browser (A07, CP21.2) resumes first, so a `prometheos start` after a
        // restart does not remember it a second time; the launch code pairs one that is not.
        if (await exchange("/device/resume", {})) return;
        if (launchCode && (await exchange("/pair", { token: launchCode }))) return;
        setCsrf(null);
        setStatus("unpaired");
      } catch (err) {
        if (err instanceof ApiError && err.kind === "offline") {
          setOffline(true);
          window.setTimeout(() => void checkSession(), 3000);
        } else {
          setStatus("unpaired");
        }
      }
    })().finally(() => {
      sessionCheck.current = null;
    });
    sessionCheck.current = task;
    return task;
  }, []);

  useEffect(() => {
    void checkSession();
  }, [checkSession]);

  useEffect(
    () =>
      onConnectionEvent((event) => {
        // A session that ends while the hub runs tries the remembered browser first (A07,
        // CP21.2; review cp21-a A7); a revoked or forgotten browser lands on pairing.
        if (event === "unpaired") void checkSession();
        else if (event === "offline") setOffline(true);
        else setOffline(false);
      }),
    [checkSession],
  );

  useEffect(() => {
    // In maintenance mode no project route answers: the recovery screen reads the maintenance
    // routes only.
    if (status === "paired" && !maintenance) void loadProject().catch(() => undefined);
  }, [status, maintenance, loadProject]);

  // Change feed: long-poll /changes; restart (cancelling the pending request) on
  // navigation; invalidate views whenever the project sequence moves.
  const projectLoaded = project !== null;
  useEffect(() => {
    if (status !== "paired" || !projectLoaded) return;
    const controller = new AbortController();
    let stopped = false;
    const loop = async () => {
      let delay = 0;
      while (!stopped) {
        if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay));
        if (stopped) return;
        try {
          const feed = await get<Changes>(
            `/changes${q({ after_seq: seqRef.current, wait: 20 })}`,
            { signal: controller.signal },
          );
          delay = 0;
          if (feed.reset) {
            seqRef.current = 0;
            await loadProject().catch(() => undefined);
            setDataVersion((v) => v + 1);
            continue;
          }
          if (feed.latest_seq !== seqRef.current) {
            seqRef.current = feed.latest_seq;
            setKnownSeq(feed.latest_seq);
            setLastChange(feed);
            void loadProject().catch(() => undefined);
            setDataVersion((v) => v + 1);
          }
        } catch (err) {
          if (stopped || (err instanceof ApiError && err.kind === "aborted")) return;
          if (err instanceof ApiError && err.kind === "unpaired") return;
          delay = err instanceof ApiError && err.kind === "offline" ? 3000 : 5000;
        }
      }
    };
    void loop();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [status, projectLoaded, path, loadProject]);

  // When connectivity returns, reload everything that may have failed meanwhile.
  const wasOffline = useRef(false);
  useEffect(() => {
    if (wasOffline.current && !offline && status === "paired") {
      setDataVersion((v) => v + 1);
      void loadProject().catch(() => undefined);
      announce("Connection to the hub restored.");
    }
    wasOffline.current = offline;
  }, [offline, status, loadProject, announce]);

  const pair = useCallback(
    async (token: string) => {
      const reply = await post<{ paired: boolean; csrf_token: string }>(
        "/pair",
        { token },
        { quietUnpaired: true },
      );
      setCsrf(reply.csrf_token);
      setStatus("paired");
      announce("Paired. The hub is ready.");
    },
    [announce],
  );

  const logout = useCallback(async () => {
    try {
      await post("/logout", {});
    } finally {
      setCsrf(null);
      setProject(null);
      setStatus("unpaired");
      announce("Signed out of this browser.");
    }
  }, [announce]);

  const refresh = useCallback(async () => {
    const seq = await loadProject();
    setDataVersion((v) => v + 1);
    return seq;
  }, [loadProject]);

  const openSource = useCallback((spec: SourceSpec, invoker?: HTMLElement | null) => {
    invokerRef.current =
      invoker ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    setSource(spec);
  }, []);

  const closeSource = useCallback(() => {
    setSource(null);
    const back = invokerRef.current;
    invokerRef.current = null;
    if (back && document.contains(back)) window.setTimeout(() => back.focus(), 0);
  }, []);

  // Close the context panel when leaving the route it was opened from.
  useEffect(() => {
    setSource(null);
  }, [path]);

  const value = useMemo<HubValue>(
    () => ({
      status,
      maintenance,
      project,
      projectError,
      knownSeq,
      dataVersion,
      offline,
      lastChange,
      pair,
      logout,
      refresh,
      announce,
      source,
      openSource,
      closeSource,
    }),
    [
      status,
      maintenance,
      project,
      projectError,
      knownSeq,
      dataVersion,
      offline,
      lastChange,
      pair,
      logout,
      refresh,
      announce,
      source,
      openSource,
      closeSource,
    ],
  );

  return (
    <HubContext.Provider value={value}>
      {children}
      <div className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
        {message}
      </div>
    </HubContext.Provider>
  );
}

export interface QueryState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/** Fetch a GET path; refetches when the project changes (dataVersion) or path changes.
 *  Keeps the previous data while refetching so views do not flash. */
export function useQuery<T>(path: string | null): QueryState<T> {
  const { dataVersion, status } = useHub();
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(path !== null);
  const [nonce, setNonce] = useState(0);
  const lastPath = useRef<string | null>(null);

  useEffect(() => {
    if (path === null || status !== "paired") {
      setLoading(false);
      return;
    }
    if (lastPath.current !== path) {
      setData(undefined);
      lastPath.current = path;
    }
    const controller = new AbortController();
    setLoading(true);
    get<T>(path, { signal: controller.signal })
      .then((value) => {
        setData(value);
        setError(null);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.kind === "aborted") return;
        setError(err);
        setLoading(false);
      });
    return () => controller.abort();
  }, [path, dataVersion, nonce, status]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data, error, loading, reload };
}

/** Pushed run progress (decision 0076): call `onChange` when a run of this hub was created or
 *  wrote (state, stage, progress). The watch is the owner's run list with `watch_after`: the
 *  hub waits on an in-process count, never the store, and answers with the count in a header.
 *  Reloads are at most one per `minIntervalMs` and only while the tab is visible, so a busy run
 *  cannot flood the store with reads; each change is followed by one later reload, for a write
 *  whose commit outlasted the hub's short settle. Runs written by another process (the CLI)
 *  need the caller's slower poll. */
export function useRunWatch(onChange: () => void, minIntervalMs = 2000): void {
  const { status } = useHub();
  const latest = useRef(onChange);
  latest.current = onChange;
  useEffect(() => {
    if (status !== "paired") return;
    const controller = new AbortController();
    let stopped = false;
    let late = 0;
    const pause = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));
    const changed = () => {
      if (stopped || document.visibilityState !== "visible") return;
      latest.current();
      window.clearTimeout(late);
      late = window.setTimeout(() => {
        if (!stopped && document.visibilityState === "visible") latest.current();
      }, 1500);
    };
    const loop = async () => {
      let after: number | null = null;
      while (!stopped) {
        try {
          const seen: { generation: number | null } = { generation: null };
          await get(`/runs${q({ limit: 1, watch_after: after ?? undefined, wait: after === null ? undefined : 20 })}`, {
            signal: controller.signal,
            onHeaders: (headers) => {
              const value = headers.get("x-prometheos-run-generation");
              seen.generation = value === null ? null : Number(value);
            },
          });
          if (stopped) return;
          const generation = seen.generation;
          if (generation === null) return; // not the owner's session: nothing to watch
          if (after !== null && generation !== after) {
            changed();
            await pause(minIntervalMs);
          }
          after = generation;
        } catch (err) {
          if (stopped || (err instanceof ApiError && (err.kind === "aborted" || err.kind === "unpaired"))) return;
          await pause(5000);
        }
      }
    };
    void loop();
    // Changes seen while the tab was hidden were not reloaded: catch up on return.
    const onVisible = () => {
      if (!stopped && document.visibilityState === "visible") latest.current();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearTimeout(late);
      document.removeEventListener("visibilitychange", onVisible);
      controller.abort();
    };
  }, [status, minIntervalMs]);
}
