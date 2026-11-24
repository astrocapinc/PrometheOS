// Pairing: the one-use code printed by `prometheos hub` is typed into a POST form.
// It is never placed in a URL (the form's method is POST even without JavaScript).
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "../api";
import { useHub } from "../hub";

export function PairingPage() {
  const { pair, offline } = useHub();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    document.title = "Pair this browser · PrometheOS hub";
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const token = code.trim();
    if (!token) {
      setError("Enter the pairing code shown in the terminal.");
      inputRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await pair(token);
    } catch (err) {
      setCode("");
      if (err instanceof ApiError && err.kind === "offline") {
        setError("The hub cannot be reached. Check that `prometheos hub` is still running.");
      } else {
        setError(
          "That code is wrong, already used or expired. Start a new pairing in the terminal and type the new code.",
        );
      }
      inputRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  return (
    <main id="main" className="pairing">
      <div className="pairing-card">
        <h1>Pair this browser</h1>
        <p>
          The hub only opens for the person at this computer. Run <code>prometheos start</code> in a terminal: it
          opens this hub already signed in. Or type the one-use code that <code>prometheos hub serve</code> printed.
        </p>
        {offline ? (
          <p className="offline-banner" role="alert">
            Offline: the hub cannot be reached.
          </p>
        ) : null}
        <form method="post" action="/" onSubmit={submit} noValidate>
          <label htmlFor="pairing-code">Pairing code</label>
          <input
            id="pairing-code"
            ref={inputRef}
            name="pairing-code"
            type="password"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            value={code}
            onChange={(event) => setCode(event.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "pairing-error pairing-help" : "pairing-help"}
            autoFocus
          />
          <p id="pairing-help" className="muted">
            The code works once and expires after a few minutes. It is not stored by this page.
          </p>
          {error ? (
            <p id="pairing-error" className="field-error" role="alert">
              {error}
            </p>
          ) : null}
          <button type="submit" className="button primary" disabled={busy} aria-busy={busy}>
            {busy ? "Pairing…" : "Pair"}
          </button>
        </form>
      </div>
    </main>
  );
}
