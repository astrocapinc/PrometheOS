// Approved research folders (A02, CP11 S11.4b; decision 0066 §2). The owner lists the folders
// PrometheOS may read and adds one: an existing local folder that is not the PrometheOS home, a
// system folder or a folder overlapping another research folder (the server checks it). Adding
// is the owner's decision (this paired session); an agent's request arrives as an approval
// request. A folder cannot be removed here: imported sources keep their binding to it.
import { useState, type FormEvent } from "react";
import { describeError, post } from "../api";
import { useHub, useQuery } from "../hub";
import { ErrorState, QueryView } from "./ui";

interface Roots {
  settings_rev: number;
  // The hub never returns local paths (a folder is named by its alias); other clients see it.
  roots: { alias: string; path?: string; available: boolean }[];
  removal: string;
}

export function ResearchFolders({ open = false }: { open?: boolean }) {
  const { announce, refresh } = useHub();
  const roots = useQuery<Roots>("/project/roots");
  const [alias, setAlias] = useState("");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const add = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const done = await post<{ alias: string; path: string }>("/project/roots", {
        alias: alias.trim(),
        path: path.trim(),
      });
      announce(`Research folder ${done.alias} added.`);
      setAlias("");
      setPath("");
      roots.reload();
      await refresh();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  const count = roots.data?.roots.length ?? 0;
  return (
    <details className="card research-folders" open={open || undefined} data-testid="research-folders">
      <summary>
        <strong>Research folders</strong> ({count}): the folders PrometheOS may read for this project
      </summary>
      <QueryView query={roots} loadingLabel="Loading the research folders">
        {(data) => (
          <>
            {data.roots.length === 0 ? (
              <p className="muted" data-testid="research-folders-none">
                No research folder yet. Add the folder that holds your papers below.
              </p>
            ) : (
              <ul className="plain-list" data-testid="research-folders-list">
                {data.roots.map((r) => (
                  <li key={r.alias} data-alias={r.alias}>
                    <strong>{r.alias}</strong>
                    {r.path ? (
                      <>
                        : <code>{r.path}</code>
                      </>
                    ) : (
                      <span className="muted"> (its path stays on this computer)</span>
                    )}
                    {r.available ? null : <span className="badge badge-review"> not found on this computer</span>}
                  </li>
                ))}
              </ul>
            )}
            <p className="muted small-print" data-testid="research-folders-removal">
              {data.removal}
            </p>
          </>
        )}
      </QueryView>
      <form onSubmit={(e) => void add(e)} className="search-form" aria-label="Add a research folder">
        <div className="field">
          <label htmlFor="research-folder-alias">Short name</label>
          <input
            id="research-folder-alias"
            value={alias}
            onChange={(e) => setAlias(e.target.value)}
            placeholder="papers"
            pattern="[a-z][a-z0-9_\-]{0,39}"
            data-testid="research-folder-alias"
          />
        </div>
        <div className="field">
          <label htmlFor="research-folder-path">Folder (full path on this computer)</label>
          <input
            id="research-folder-path"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="C:\Users\you\Documents\Papers"
            data-testid="research-folder-path"
          />
        </div>
        <button
          type="submit"
          className="button primary"
          disabled={busy || !alias.trim() || !path.trim()}
          data-testid="research-folder-add"
        >
          Add folder
        </button>
      </form>
      {error ? <ErrorState error={error} /> : null}
    </details>
  );
}
