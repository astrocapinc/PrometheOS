// The read-only Zotero import (A02, CP11 S11.5; decisions 0069 and 0070), a panel of the Library.
// PrometheOS reads the owner's Zotero through its local API on this computer: no account, no key,
// nothing written to Zotero. The panel states what the probe found, plans an import of the
// library, one collection or typed item keys, shows each item with what it would do, and commits
// the selected items. All rules (loopback only, GET only, identifiers, duplicates) are the
// server's; this panel shows its refusals verbatim. Zotero text is untrusted data, rendered as
// plain text.
import { useMemo, useState, type FormEvent } from "react";
import { describeError, runOperation } from "../api";
import { useHub, useQuery } from "../hub";
import type { ZoteroCommit, ZoteroPlan, ZoteroPlanItem, ZoteroStatus } from "../types";
import { ErrorState } from "./ui";

const ACTION_TEXT: Record<string, string> = {
  create: "New work",
  merge: "Same identifier as a work: merged into it",
  update: "Newer version: its work revised",
  unchanged: "Already recorded",
  refused: "Refused",
};
const IMPORTABLE = ["create", "merge", "update"];
const KEY_RE = /^[A-Z0-9]{8}$/;

type Scope = "library" | "collection" | "items";

export function ZoteroImport({ onCommitted }: { onCommitted: () => void }) {
  const { announce } = useHub();
  const status = useQuery<ZoteroStatus>("/zotero/status?collections=true");
  const [scope, setScope] = useState<Scope>("library");
  const [collection, setCollection] = useState("");
  const [typed, setTyped] = useState("");
  const [plan, setPlan] = useState<ZoteroPlan | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [commit, setCommit] = useState<ZoteroCommit | null>(null);
  const [busy, setBusy] = useState<"plan" | "commit" | null>(null);
  const [error, setError] = useState<unknown>(null);

  const state = status.data?.state;
  const collections = status.data?.collections ?? [];
  const chosen = collection || collections[0]?.key || "";
  const typedKeys = useMemo(
    () => typed.split(/[\s,;]+/).map((k) => k.trim().toUpperCase()).filter(Boolean),
    [typed],
  );
  const keysOk = typedKeys.length > 0 && typedKeys.every((k) => KEY_RE.test(k));

  const makePlan = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setCommit(null);
    setBusy("plan");
    try {
      const body: Record<string, unknown> = {};
      if (scope === "collection") body.collection = chosen;
      if (scope === "items") body.item_keys = typedKeys;
      const made = await runOperation<ZoteroPlan>("/zotero/plans", body);
      setPlan(made);
      setPicked(new Set(made.items.filter((i) => IMPORTABLE.includes(i.action)).map((i) => i.key)));
      announce(`Zotero plan ready: ${made.items.length} item(s).`);
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
      status.reload(); // the probe may have changed (Zotero closed, the API turned off)
    } finally {
      setBusy(null);
    }
  };

  const commitSelected = async () => {
    if (!plan) return;
    setError(null);
    setBusy("commit");
    try {
      const all = plan.items.filter((i) => IMPORTABLE.includes(i.action)).length;
      const body: Record<string, unknown> = picked.size === all ? {} : { item_keys: [...picked].sort() };
      const done = await runOperation<ZoteroCommit>(
        `/zotero/plans/${encodeURIComponent(plan.plan_id)}/commit`,
        body,
      );
      setCommit(done);
      announce(`Zotero import committed: ${done.counts.committed ?? 0} item(s).`);
      onCommitted();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
      status.reload();
    } finally {
      setBusy(null);
    }
  };

  const toggle = (key: string) => {
    const next = new Set(picked);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setPicked(next);
  };

  const importable = plan ? plan.items.filter((i) => IMPORTABLE.includes(i.action)) : [];
  return (
    <section className="card" aria-labelledby="zotero-heading" data-testid="zotero-panel">
      <h2 id="zotero-heading">Import from Zotero</h2>
      <p className="muted">
        Reading only: PrometheOS asks the Zotero on this computer for its items (no account, no key) and never
        changes Zotero. An item's text is kept as data; a work that two sources describe stays one work.
      </p>
      <p data-testid="zotero-status" data-state={state ?? "loading"} role="status">
        {status.loading && !status.data
          ? "Checking Zotero…"
          : status.data
            ? status.data.text
            : status.error
              ? describeError(status.error).title
              : ""}
        {status.data?.state === "available" && status.data.version && status.data.version !== "unknown"
          ? ` (version ${status.data.version})`
          : ""}{" "}
        <button type="button" className="link-button" onClick={status.reload} data-testid="zotero-recheck">
          Check again
        </button>
      </p>
      {status.data?.detail && state !== "available" ? <p className="muted">{status.data.detail}</p> : null}
      <form onSubmit={(e) => void makePlan(e)} className="search-form" aria-label="Plan a Zotero import">
        <div className="field">
          <label htmlFor="zotero-scope">What to read</label>
          <select
            id="zotero-scope"
            value={scope}
            onChange={(e) => setScope(e.target.value as Scope)}
            data-testid="zotero-scope"
          >
            <option value="library">The whole library</option>
            <option value="collection" disabled={collections.length === 0}>
              One collection
            </option>
            <option value="items">Named items</option>
          </select>
        </div>
        {scope === "collection" ? (
          <div className="field">
            <label htmlFor="zotero-collection">Collection</label>
            <select
              id="zotero-collection"
              value={chosen}
              onChange={(e) => setCollection(e.target.value)}
              data-testid="zotero-collection"
            >
              {collections.map((c) => (
                <option key={c.key} value={c.key}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        {scope === "items" ? (
          <div className="field grow">
            <label htmlFor="zotero-keys">Item keys (8 characters each, separated by spaces)</label>
            <input
              id="zotero-keys"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder="ABCD1234 EFGH5678"
              data-testid="zotero-keys"
            />
          </div>
        ) : null}
        <button
          type="submit"
          className="button secondary"
          disabled={busy !== null || state !== "available" || (scope === "items" && !keysOk)}
          data-testid="zotero-plan"
        >
          {busy === "plan" ? "Planning…" : "Plan import"}
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
      {plan ? (
        <div data-testid="zotero-plan-result" data-plan-id={plan.plan_id}>
          <h3>Plan</h3>
          <p data-testid="zotero-plan-counts">
            {plan.counts.create ?? 0} new, {plan.counts.merge ?? 0} merged into a work, {plan.counts.update ?? 0}{" "}
            newer, {plan.counts.unchanged ?? 0} already recorded, {plan.counts.refused ?? 0} refused;{" "}
            {Object.values(plan.skipped).reduce((a, b) => a + b, 0)} attachment(s), note(s) or annotation(s) not
            imported
            {plan.more ? `; ${plan.more} more item(s) left for a later plan` : ""}.
          </p>
          <div className="table-wrap">
            <table className="data-table files-table" data-testid="zotero-plan-items">
              <thead>
                <tr>
                  <th scope="col">Import</th>
                  <th scope="col">Item</th>
                  <th scope="col">Title</th>
                  <th scope="col">Action</th>
                  <th scope="col">Identifiers</th>
                </tr>
              </thead>
              <tbody>
                {plan.items.map((item: ZoteroPlanItem) => (
                  <tr key={item.key} data-key={item.key} data-action={item.action}>
                    <td>
                      {IMPORTABLE.includes(item.action) ? (
                        <input
                          type="checkbox"
                          checked={picked.has(item.key)}
                          onChange={() => toggle(item.key)}
                          aria-label={`Import ${item.key}`}
                          data-testid="zotero-pick"
                        />
                      ) : null}
                    </td>
                    <td>
                      <code>{item.key}</code> <span className="muted">v{item.version}</span>
                    </td>
                    <td>
                      {item.title ?? <span className="muted">(no title)</span>}{" "}
                      <span className="muted">
                        {item.item_type}
                        {item.has_abstract ? ", abstract" : ""}
                      </span>
                    </td>
                    <td>
                      {ACTION_TEXT[item.action] ?? item.action}
                      {item.reason ? <span className="muted"> · {item.reason}</span> : null}
                    </td>
                    <td>
                      {Object.entries(item.identifiers).map(([k, v]) => (
                        <div key={k}>
                          {k.toUpperCase()} {v}
                        </div>
                      ))}
                      {Object.entries(item.related).map(([k, v]) => (
                        <div key={k} className="muted">
                          names {k.replace(/_/g, " ")} {v}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {plan.refused.length > 0 ? (
            <ul className="plain-list" data-testid="zotero-refused">
              {plan.refused.map((r, n) => (
                <li key={`${r.key ?? "?"}:${n}`} data-reason={r.reason}>
                  <code>{r.key ?? "(no key)"}</code>: {r.detail}
                </li>
              ))}
            </ul>
          ) : null}
          <div className="button-row">
            <button
              type="button"
              className="button primary"
              disabled={busy !== null || picked.size === 0 || commit !== null}
              onClick={() => void commitSelected()}
              data-testid="zotero-commit"
            >
              {busy === "commit" ? "Importing…" : `Import ${picked.size} of ${importable.length} item(s)`}
            </button>
          </div>
        </div>
      ) : null}
      {commit ? (
        <p data-testid="zotero-commit-result" role="status">
          Imported {commit.counts.committed ?? 0} item(s); {commit.counts.skipped ?? 0} skipped,{" "}
          {commit.counts.unchanged ?? 0} already recorded, {commit.counts.not_selected ?? 0} not selected.
          {commit.outcomes
            .filter((o) => o.outcome === "skipped")
            .map((o) => ` ${o.key}: ${o.detail ?? o.reason}.`)
            .join("")}
        </p>
      ) : null}
    </section>
  );
}
