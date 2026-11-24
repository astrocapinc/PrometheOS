// The command palette (Ctrl K) and keyboard shortcuts. The palette only navigates, hands text
// to a page's own form, changes the theme or reads the existing knowledge and library search:
// it holds no business logic and calls no write operation (spec/06).
import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { ApiError, get, q } from "../api";
import { recordPath } from "../data";
import { handOff, type HandoffTarget } from "../handoff";
import { useHub } from "../hub";
import { ALL_PAGES } from "../nav";
import { match, useRouter } from "../router";
import { THEME_LABELS, setTheme, type ThemeChoice } from "../theme";
import type { LibrarySearch, SearchHit } from "../types";
import { Icon, type IconName } from "./icons";
import { kindLabel } from "./ui";

/** Every record kind the project search indexes (store/derived.py SEARCH_FIELDS), by icon. */
const RECORD_ICONS: Record<string, IconName> = {
  entry: "notebook",
  claim: "knowledge",
  correction: "knowledge",
  item: "inbox",
  meeting: "inbox",
  action: "work",
  paper: "papers",
  anchor: "papers",
  source: "artifacts",
  version: "artifacts",
  work: "library",
  execution: "runs",
  run: "runs",
  person: "knowledge",
};

type Opener = (which: "palette" | "shortcuts") => void;
const openers = new Set<Opener>();

/** Open the palette or the shortcut sheet from anywhere (the top bar's search button). */
export function openOverlay(which: "palette" | "shortcuts") {
  openers.forEach((open) => open(which));
}

interface Command {
  id: string;
  group: "Actions" | "Pages" | "Records" | "Library" | "Theme";
  label: string;
  hint?: string;
  icon: IconName;
  keywords?: string;
  run: () => void;
}

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  const role = target.getAttribute("role");
  return role === "textbox" || role === "combobox" || role === "searchbox";
}

/** The element focus is really in, through open shadow roots (the LaTeX editor lives in one). */
function deepActiveElement(): Element | null {
  let active = document.activeElement;
  while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
  return active;
}

/** Bare-key shortcuts (g then a letter, ?) stand down whenever the owner may be typing.
 *  A key event from inside a shadow root reaches this window listener retargeted to the
 *  shadow host (a plain div), so the event's own target is not enough: its composed path, the
 *  deep active element, any editor host and the LaTeX workspace page are all checked
 *  (regression int11: typing "% cited" in the workspace editor opened Capture). */
function typingContext(event: KeyboardEvent): boolean {
  const path = event.composedPath();
  if (path.some((node) => isEditable(node))) return true;
  if (isEditable(deepActiveElement())) return true;
  if (path.some((node) => node instanceof HTMLElement && node.closest(".tex-editor, .cm-editor, [data-shortcuts=off]"))) {
    return true;
  }
  if (match("/papers/:id/workspace", window.location.pathname)) return true;
  return document.querySelector("dialog[open]") !== null;
}

function matches(command: Command, words: string[]): number {
  if (words.length === 0) return 1;
  const label = command.label.toLowerCase();
  const hay = `${label} ${command.keywords ?? ""} ${command.group}`.toLowerCase();
  if (!words.every((w) => hay.includes(w))) return 0;
  const first = words[0] ?? "";
  return label.startsWith(first) ? 3 : label.includes(first) ? 2 : 1;
}

/** Highlight the query's words inside a label (text nodes only). */
function Highlight({ text, words }: { text: string; words: string[] }) {
  if (words.length === 0) return <>{text}</>;
  const lower = text.toLowerCase();
  const marks: [number, number][] = [];
  for (const w of words) {
    const at = lower.indexOf(w);
    if (at >= 0) marks.push([at, at + w.length]);
  }
  marks.sort((a, b) => a[0] - b[0]);
  const parts: ReactNode[] = [];
  let pos = 0;
  marks.forEach(([start, end], i) => {
    if (start < pos) return;
    if (start > pos) parts.push(text.slice(pos, start));
    parts.push(<mark key={i}>{text.slice(start, end)}</mark>);
    pos = end;
  });
  parts.push(text.slice(pos));
  return <>{parts}</>;
}

interface Found {
  query: string;
  records: SearchHit[];
  library: LibrarySearch["hits"];
  failed: boolean;
}

export function CommandLayer() {
  const { navigate } = useRouter();
  const { logout, openSource, status } = useHub();
  const paletteRef = useRef<HTMLDialogElement>(null);
  const helpRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const [open, setOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [text, setText] = useState("");
  const [active, setActive] = useState(0);
  const [found, setFound] = useState<Found | null>(null);
  const [searching, setSearching] = useState(false);
  const listId = useId();
  const optionId = (i: number) => `${listId}-opt-${i}`;

  const show = useCallback((which: "palette" | "shortcuts") => {
    const dialog = which === "palette" ? paletteRef.current : helpRef.current;
    if (!dialog || dialog.open) return;
    paletteRef.current?.open && paletteRef.current.close();
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.showModal();
    if (which === "shortcuts") setHelpOpen(true);
    if (which === "palette") {
      setText("");
      setActive(0);
      setFound(null);
      setOpen(true);
      inputRef.current?.focus();
    }
  }, []);

  const close = useCallback(() => {
    paletteRef.current?.open && paletteRef.current.close();
  }, []);

  // Global keys: Ctrl/Cmd+K anywhere; "g" then a letter, "c" and "?" outside text fields.
  useEffect(() => {
    openers.add(show);
    let pendingG = 0;
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (paletteRef.current?.open) close();
        else show("palette");
        return;
      }
      if (event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
      if (event.repeat || event.isComposing || typingContext(event)) {
        pendingG = 0;
        return;
      }
      const key = event.key.toLowerCase();
      if (pendingG && Date.now() - pendingG < 1500) {
        pendingG = 0;
        const page = ALL_PAGES.find((p) => p.key === key);
        if (page) {
          event.preventDefault();
          navigate(page.to);
        }
        return;
      }
      // No bare single-letter actions: only "g" then a letter (Capture is g c) and "?".
      if (key === "g") {
        pendingG = Date.now();
      } else if (event.key === "?") {
        event.preventDefault();
        show("shortcuts");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      openers.delete(show);
      window.removeEventListener("keydown", onKey);
    };
  }, [show, close, navigate]);

  // Search records once the query has settled (the same reads the Knowledge and Library pages make).
  const query = text.trim();
  useEffect(() => {
    if (!open || query.length < 2 || status !== "paired") {
      setFound(null);
      setSearching(false);
      return;
    }
    const controller = new AbortController();
    setSearching(true);
    const timer = window.setTimeout(() => {
      Promise.allSettled([
        get<{ hits: SearchHit[] }>(`/knowledge/search${q({ q: query, limit: 9 })}`, { signal: controller.signal }),
        get<LibrarySearch>(`/library/search${q({ query, limit: 4 })}`, { signal: controller.signal }),
      ]).then(([records, library]) => {
        if (controller.signal.aborted) return;
        const aborted = (r: PromiseSettledResult<unknown>) =>
          r.status === "rejected" && r.reason instanceof ApiError && r.reason.kind === "aborted";
        if (aborted(records) || aborted(library)) return;
        setFound({
          query,
          records:
            records.status === "fulfilled" ? records.value.hits.filter((h) => h.kind !== "project").slice(0, 8) : [],
          library: library.status === "fulfilled" ? library.value.hits.slice(0, 4) : [],
          failed: records.status === "rejected" && library.status === "rejected",
        });
        setSearching(false);
      });
    }, 220);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [open, query, status]);

  const go = useCallback(
    (to: string, target?: HandoffTarget, value?: string) => {
      if (target && value) handOff(target, value);
      // The new page's heading takes focus (App), so the palette does not hand it back.
      returnFocus.current = null;
      close();
      navigate(to);
    },
    [close, navigate],
  );

  const commands = useMemo(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    const list: Command[] = [];
    const quoted = query ? `“${query}”` : "";
    list.push({
      id: "capture",
      group: "Actions",
      label: query ? `Capture a note: ${quoted}` : "Capture a note or meeting",
      hint: "G C",
      icon: "plus",
      keywords: "capture new note meeting record",
      run: () => go("/capture", "capture", query),
    });
    list.push({
      id: "work",
      group: "Actions",
      label: query ? `Add a work item: ${quoted}` : "Add a work item",
      icon: "work",
      keywords: "add new work item task deadline",
      run: () => go("/work", "work", query),
    });
    if (query) {
      list.push({
        id: "search-knowledge",
        group: "Actions",
        label: `Search knowledge for ${quoted}`,
        icon: "search",
        run: () => go("/knowledge", "knowledge", query),
      });
      list.push({
        id: "search-library",
        group: "Actions",
        label: `Search the library for ${quoted}`,
        icon: "library",
        run: () => go("/knowledge/library", "library", query),
      });
    }
    const pageCommands: Command[] = ALL_PAGES.filter((p) => p.to !== "/capture").map((p) => ({
        id: `page:${p.to}`,
        group: "Pages",
        label: p.label,
        hint: p.key ? `G ${p.key.toUpperCase()}` : undefined,
        icon: p.icon,
        keywords: `go open page ${p.keywords ?? ""}`,
        run: () => go(p.to),
      }));
    const other: Command[] = [
      ...(["dark", "light", "system"] as ThemeChoice[]).map(
        (choice): Command => ({
          id: `theme:${choice}`,
          group: "Theme",
          label: choice === "system" ? "Theme: follow Windows" : `Theme: ${THEME_LABELS[choice].toLowerCase()}`,
          icon: choice === "dark" ? "moon" : choice === "light" ? "sun" : "monitor",
          keywords: "theme appearance colour color mode",
          run: () => {
            setTheme(choice);
            close();
          },
        }),
      ),
      {
        id: "shortcuts",
        group: "Actions",
        label: "Keyboard shortcuts",
        hint: "?",
        icon: "keyboard",
        keywords: "help keys",
        run: () => {
          close();
          show("shortcuts");
        },
      },
      {
        id: "agent",
        group: "Actions",
        label: "Connect an agent",
        icon: "plug",
        keywords: "claude code opencode mcp plugin setup",
        run: () => {
          returnFocus.current = null;
          close();
          const guide = document.querySelector<HTMLDetailsElement>("details.agent-guide");
          if (guide) {
            guide.open = true;
            guide.querySelector("summary")?.focus();
          }
        },
      },
      {
        id: "signout",
        group: "Actions",
        label: "Sign out",
        icon: "signout",
        keywords: "log out logout",
        run: () => {
          close();
          void logout();
        },
      },
    ];
    const scored = [...list, ...pageCommands, ...other]
      .map((c) => ({ c, score: c.id === "capture" || c.id === "work" || c.id.startsWith("search-") ? (query ? 4 : 1) : matches(c, words) }))
      .filter((s) => s.score > 0);
    // With no query: actions, then pages. With a query, matching pages come first, so typing a
    // page's name and pressing Enter opens it; best label matches lead within a group.
    const order: Command["group"][] = query
      ? ["Pages", "Actions", "Records", "Library", "Theme"]
      : ["Actions", "Pages", "Records", "Library", "Theme"];
    const result = scored
      .sort((a, b) => order.indexOf(a.c.group) - order.indexOf(b.c.group) || b.score - a.score)
      .map((s) => s.c)
      .filter((c) => (query ? true : c.group !== "Theme"));
    if (found && found.query === query) {
      for (const hit of found.records) {
        const path = recordPath({ id: hit.id, kind: hit.kind, body: {} });
        result.push({
          id: `rec:${hit.id}`,
          group: "Records",
          label: hit.title || hit.excerpt || hit.id,
          hint: kindLabel(hit.kind),
          icon: RECORD_ICONS[hit.kind] ?? "knowledge",
          run: () => {
            if (hit.kind === "version") {
              close();
              openSource({ versionId: hit.id, title: "Source text match" }, returnFocus.current);
            } else if (hit.kind === "work") {
              go("/knowledge/library", "library", hit.title || query); // a library work: its page text
            } else if (hit.kind === "execution") {
              go("/now"); // registered runs are listed on Now
            } else if (path && hit.kind !== "item") go(path);
            else go("/knowledge", "knowledge", query); // the record in the full search list
          },
        });
      }
      for (const hit of found.library) {
        result.push({
          id: `lib:${hit.work_id}:${hit.page}:${hit.start}`,
          group: "Library",
          label: hit.title || "Untitled work",
          hint: `p. ${hit.page}`,
          icon: "library",
          run: () => go("/knowledge/library", "library", query),
        });
      }
    }
    return { list: result, words };
  }, [query, found, go, close, show, logout, openSource]);

  const items = commands.list;
  const current = Math.min(active, Math.max(items.length - 1, 0));

  useEffect(() => {
    document.getElementById(optionId(current))?.scrollIntoView({ block: "nearest" });
  }, [current]);

  const onInputKey = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((current + 1) % Math.max(items.length, 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((current - 1 + items.length) % Math.max(items.length, 1));
    } else if (event.key === "Home") {
      event.preventDefault();
      setActive(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActive(Math.max(items.length - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      items[current]?.run();
    } else if (event.key === "Tab") {
      event.preventDefault();
    }
  };

  const groups: { name: Command["group"]; start: number; items: Command[] }[] = [];
  items.forEach((item, i) => {
    const last = groups[groups.length - 1];
    if (last && last.name === item.group) last.items.push(item);
    else groups.push({ name: item.group, start: i, items: [item] });
  });

  return (
    <>
      <dialog
        ref={paletteRef}
        className="palette"
        aria-label="Command palette"
        data-testid="command-palette"
        onClose={() => {
          setOpen(false);
          returnFocus.current?.isConnected && returnFocus.current.focus();
        }}
        onClick={(event) => {
          if (event.target === paletteRef.current) close();
        }}
      >
        <div className="palette-body">
          <div className="palette-query">
            <Icon name="search" size={18} />
            <input
              ref={inputRef}
              type="text"
              role="combobox"
              aria-label="Search pages, actions and records"
              aria-expanded="true"
              aria-controls={listId}
              aria-autocomplete="list"
              aria-activedescendant={items.length ? optionId(current) : undefined}
              placeholder="Search or jump to…"
              value={text}
              autoComplete="off"
              spellCheck={false}
              maxLength={200}
              onChange={(e) => {
                setText(e.target.value);
                setActive(0);
              }}
              onKeyDown={onInputKey}
            />
            <kbd className="kbd">Esc</kbd>
          </div>
          {open ? (
          <div className="palette-list" id={listId} role="listbox" aria-label="Results">
            {groups.map((group) => (
              <div role="group" aria-label={group.name} key={`${group.name}-${group.start}`} className="palette-group">
                <div className="palette-group-label" aria-hidden="true">
                  {group.name === "Records" ? "Records: notes, claims, meetings, work, papers" : group.name}
                </div>
                {group.items.map((item, j) => {
                  const index = group.start + j;
                  return (
                    <div
                      key={item.id}
                      id={optionId(index)}
                      role="option"
                      aria-selected={index === current}
                      className={index === current ? "palette-item active" : "palette-item"}
                      data-command={item.id}
                      onMouseMove={() => index !== current && setActive(index)}
                      onClick={() => item.run()}
                    >
                      <Icon name={item.icon} />
                      <span className="palette-label">
                        <Highlight text={item.label} words={item.group === "Actions" ? [] : commands.words} />
                      </span>
                      {item.hint ? <span className="palette-hint">{item.hint}</span> : null}
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
          ) : null}
          {open && items.length === 0 ? <p className="palette-empty">Nothing matches “{query}”.</p> : null}
          <div className="palette-foot">
            <span>
              <kbd className="kbd">↑</kbd> <kbd className="kbd">↓</kbd> move
            </span>
            <span>
              <kbd className="kbd">Enter</kbd> open
            </span>
            <span className="palette-status" role="status">
              {searching
                ? "Searching this project…"
                : found?.failed
                  ? "Search is unavailable right now."
                  : query.length >= 2 && found
                    ? `${found.records.length + found.library.length} matching record${found.records.length + found.library.length === 1 ? "" : "s"}`
                    : "Searches every record and the library on this PC"}
            </span>
          </div>
        </div>
      </dialog>
      <dialog
        ref={helpRef}
        className="shortcuts"
        aria-labelledby="shortcuts-title"
        data-testid="shortcuts-dialog"
        onClose={() => {
          setHelpOpen(false);
          returnFocus.current?.isConnected && returnFocus.current.focus();
        }}
        onClick={(event) => {
          if (event.target === helpRef.current) helpRef.current?.close();
        }}
      >
        {helpOpen ? (
        <div className="shortcuts-body">
          <div className="shortcuts-head">
            <h2 id="shortcuts-title">Keyboard shortcuts</h2>
            <button type="button" className="icon-button" aria-label="Close" onClick={() => helpRef.current?.close()}>
              <Icon name="close" />
            </button>
          </div>
          <dl className="shortcut-list">
            <div>
              <dt>
                <kbd className="kbd">Ctrl</kbd> <kbd className="kbd">K</kbd>
              </dt>
              <dd>Search, jump to a page or run an action</dd>
            </div>
            {ALL_PAGES.filter((p) => p.key).map((p) => (
              <div key={p.to}>
                <dt>
                  <kbd className="kbd">G</kbd> <kbd className="kbd">{p.key?.toUpperCase()}</kbd>
                </dt>
                <dd>Go to {p.label}</dd>
              </div>
            ))}
            <div>
              <dt>
                <kbd className="kbd">Esc</kbd>
              </dt>
              <dd>Close the palette, a dialog or the source panel</dd>
            </div>
            <div>
              <dt>
                <kbd className="kbd">?</kbd>
              </dt>
              <dd>Show this list</dd>
            </div>
            <div>
              <dt>
                <span className="muted small-print">While typing</span>
              </dt>
              <dd>Letter shortcuts are off in text fields, editors, dialogs and the LaTeX workspace</dd>
            </div>
          </dl>
        </div>
        ) : null}
      </dialog>
    </>
  );
}
