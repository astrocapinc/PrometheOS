// The recovery screen (A02, CP10.5; spec/06 journey 11; decision 0021). It appears before any
// destination when the project store does not open, and at /recovery otherwise. Every step
// calls a maintenance route that opens no project database: inspect the store, verify a backup
// in a staging folder, restore it into a NEW store (the damaged store is never written), then
// switch the registry to it, or grant an agent's pending request to switch. The damaged store
// stays listed for inspection. Locations are labels: no local path reaches the browser.
import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, post } from "../api";
import { Details, Empty, ErrorState, KeyValues, PageHeading, QueryView, StateBadge, formatDate } from "../components/ui";
import { useHub, useQuery } from "../hub";
import type {
  BackupVerified,
  MaintenanceApprovalView,
  MaintenanceInspect,
  MaintenanceRestored,
  QuarantineView,
} from "../types";

const POLL_MS = 5000;

/** Health a switch may replace (decision 0021 round 2, F2): the store does not open. */
const DAMAGED = new Set(["corrupt", "unreadable", "missing", "not_a_store", "wrong_project", "needs_journal_recovery"]);

const HEALTH_TEXT: Record<string, string> = {
  ok: "Healthy: the store opens normally.",
  needs_journal_recovery:
    "Interrupted write: a rollback journal was left behind. Recover it into a new store; it is never applied to this store.",
  corrupt: "Damaged: the database fails its integrity check.",
  unreadable: "Unreadable: the database file cannot be read.",
  missing: "Missing: the store's database is not there.",
  needs_migration: "Older schema: the store needs a migration before it opens.",
  newer_schema: "Newer schema: written by a newer PrometheOS; this version will not open it.",
  not_a_store: "Not a PrometheOS store.",
  wrong_project: "The registered store holds a different project.",
};

const OPERATION_TEXT: Record<string, string> = {
  maintenance_switch: "Switch the project to a restored store",
  maintenance_migrate: "Migrate the store's schema",
};

function short(digest: string | null | undefined): string {
  return digest ? `${digest.slice(0, 12)}…` : "—";
}

function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

function Health({ data }: { data: MaintenanceInspect }) {
  const db = data.database;
  return (
    <section aria-labelledby="health-title" data-testid="recovery-health" data-health={data.health}>
      <h2 id="health-title">Store health</h2>
      <p className={data.opens ? "muted" : "inline-warning"} data-testid="recovery-health-text">
        {HEALTH_TEXT[data.health] ?? data.health}
      </p>
      <KeyValues
        rows={[
          ["Store", <span data-testid="recovery-store-label">{data.store.label}</span>],
          ["Integrity", <span data-testid="recovery-integrity">{db.integrity}</span>],
          ["Detail", db.integrity_detail ?? "—"],
          ["Schema version", `${db.schema_version ?? "unknown"} (this version opens ${db.supported_schema_version})`],
          ["Project sequence", db.project_seq ?? "—"],
          ["Database digest", <code data-testid="recovery-db-digest">{db.sha256}</code>],
          ["Database size", bytes(db.bytes)],
          ["Maintenance lock", <span data-testid="recovery-lock">{data.lock.maintenance}</span>],
          ["Journal", db.journal.state === "hot" ? "interrupted write (hot rollback journal)" : db.journal.wal ? "write-ahead log present" : db.journal.rollback_journal ? "rollback journal present" : "none"],
        ]}
      />
      {db.journal_recovery ? (
        <p className="muted" data-testid="recovery-journal-preview">
          Recovering the journal (on a copy) gives a store that is {db.journal_recovery.health} at project sequence{" "}
          {db.journal_recovery.project_seq ?? "unknown"}.
        </p>
      ) : null}
      <Details label="Environment (doctor)">
        <dl className="kv" data-testid="recovery-environment">
          {Object.entries(data.environment)
            .filter(([, v]) => typeof v !== "object")
            .map(([k, v]) => (
              <div key={k} className="kv-row">
                <dt>{k}</dt>
                <dd>{String(v)}</dd>
              </div>
            ))}
        </dl>
      </Details>
    </section>
  );
}

function BackupRow({
  name,
  size,
  modified,
  onRestored,
}: {
  name: string;
  size: number;
  modified: string;
  onRestored: (r: MaintenanceRestored) => void;
}) {
  const { announce } = useHub();
  const [verified, setVerified] = useState<BackupVerified | null>(null);
  const [busy, setBusy] = useState<"verify" | "restore" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const run = async (verb: "verify" | "restore") => {
    setBusy(verb);
    setError(null);
    try {
      if (verb === "verify") {
        const result = await post<BackupVerified>("/maintenance/verify", { backup: name });
        setVerified(result);
        announce(`Backup ${name} verified.`);
      } else {
        const result = await post<MaintenanceRestored>("/maintenance/restore", { backup: name });
        announce(`Restored into a new store: ${result.target_store_label}.`);
        onRestored(result);
      }
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };
  return (
    <li data-testid="recovery-backup" data-backup={name}>
      <p>
        <strong>{name}</strong> <span className="muted">· {bytes(size)} · {formatDate(modified)}</span>
      </p>
      {verified ? (
        <div data-testid="recovery-verified">
          <p>
            <StateBadge state="completed" prefix="Verification" /> Verified in a staging folder: project sequence{" "}
            {verified.manifest.project_seq}, schema {verified.manifest.schema_version}, {verified.manifest.media_count} media
            file(s); manifest <code>{short(verified.backup_manifest_sha256)}</code>.
          </p>
          {verified.manifest.reference_only_count > 0 ? (
            <p className="muted">
              {verified.manifest.reference_only_count} referenced source(s) were never part of the backup (reference only).
            </p>
          ) : null}
        </div>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      <div className="button-row">
        <button type="button" className="button secondary" onClick={() => void run("verify")} disabled={busy !== null} aria-busy={busy === "verify"} data-testid="recovery-verify">
          {busy === "verify" ? "Verifying…" : "Verify this backup"}
        </button>
        <button
          type="button"
          className="button primary"
          onClick={() => void run("restore")}
          disabled={busy !== null || verified === null}
          aria-busy={busy === "restore"}
          title={verified ? undefined : "Verify the backup first"}
          data-testid="recovery-restore"
        >
          {busy === "restore" ? "Restoring…" : "Restore into a new store"}
        </button>
      </div>
    </li>
  );
}

function Restored({
  restored,
  health,
  onSwitched,
}: {
  restored: MaintenanceRestored;
  health: string;
  onSwitched: () => void;
}) {
  const { announce } = useHub();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const switchNow = async () => {
    setBusy(true);
    setError(null);
    try {
      await post("/maintenance/switch", { backup: restored.backup.name, target: restored.target_store_label });
      setDone(true);
      announce("Switched. The damaged store is kept and listed as quarantined.");
      onSwitched();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(false);
    }
  };
  return (
    <section aria-labelledby="restored-title" data-testid="recovery-restored" className="decision">
      <h2 id="restored-title">Restored into a new store</h2>
      <p>
        <span data-testid="recovery-target-label">{restored.target_store_label}</span> holds backup {restored.backup.name}{" "}
        (project sequence {restored.manifest.project_seq}). The registry still points at{" "}
        {restored.damaged_store_label}; nothing was written there.
      </p>
      {error ? <ErrorState error={error} /> : null}
      {!restored.target_managed ? (
        <p className="inline-warning">This store is outside the PrometheOS managed stores area.</p>
      ) : null}
      {done ? (
        <p data-testid="recovery-switched">Switched. Restart the hub to open the project from its restored store.</p>
      ) : !DAMAGED.has(health) ? (
        <p className="muted" data-testid="recovery-no-switch">
          The current store is not damaged ({health}), so it is not switched away from here. A deliberate rollback to
          a backup is done in a terminal with <code>prometheos backup restore</code>, which shows the current and the
          backup's sequence and asks you to confirm the rollback.
        </p>
      ) : (
        <button type="button" className="button primary" onClick={() => void switchNow()} disabled={busy} aria-busy={busy} data-testid="recovery-switch">
          {busy ? "Verifying and switching…" : "Switch the project to this store"}
        </button>
      )}
    </section>
  );
}

function RequestCard({
  approval,
  current,
  onDone,
}: {
  approval: MaintenanceApprovalView;
  current: MaintenanceInspect;
  onDone: () => void;
}) {
  const { announce } = useHub();
  const [busy, setBusy] = useState<"grant" | "deny" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const id = approval.approval_request_id;
  const decide = async (verb: "grant" | "deny") => {
    setBusy(verb);
    setError(null);
    try {
      await post(`/maintenance/approvals/${id}/${verb}`, { plan_sha256: approval.plan_sha256 });
      announce(verb === "grant" ? "Granted. The agent can now resume exactly this request once." : "Denied.");
      onDone();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };
  return (
    <article className="decision approval-card" data-testid="recovery-request" data-approval-id={id} data-state={approval.state} aria-labelledby={`${id}-title`}>
      <h3 id={`${id}-title`}>{OPERATION_TEXT[approval.operation] ?? approval.operation}</h3>
      <p>
        <StateBadge state={approval.state} />
      </p>
      <KeyValues
        rows={[
          ["Requested by", `${approval.actor} (${approval.caller_rank === 0 ? "agent scope" : "owner scope"})`],
          ["Requested", formatDate(approval.created_at)],
          ["Expires", formatDate(approval.expires_at)],
          ["Current store", `${approval.damaged_store_label} (database ${short(approval.damaged_db_sha256)})`],
          [
            "Current store health",
            <span data-testid="recovery-request-health">
              {current.health} now; {String(approval.plan.store_health ?? "—")} when requested
            </span>,
          ],
          [
            "Latest sequence",
            String(current.database.project_seq ?? approval.plan.store_project_seq ?? "unknown (the store does not open)"),
          ],
          [
            "Backup",
            approval.plan.backup_name
              ? `${String(approval.plan.backup_name)} of ${formatDate(String(approval.plan.backup_created_at ?? ""))}, project sequence ${String(approval.plan.project_seq)}`
              : "—",
          ],
          ["Verified backup manifest", short(approval.backup_manifest_sha256)],
          ["Becomes live", <span data-testid="recovery-request-target">{approval.target_store_label ?? "—"}</span>],
        ]}
      />
      <p className="decision-label">What it would do:</p>
      <ul className="effects" data-testid="recovery-request-summary">
        {approval.summary.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
      <p>
        Plan digest: <code data-testid="recovery-request-digest">{approval.plan_sha256}</code>
      </p>
      {error ? <ErrorState error={error} /> : null}
      {approval.state === "pending" ? (
        <div className="button-row">
          <button type="button" className="button primary" onClick={() => void decide("grant")} disabled={busy !== null} aria-busy={busy === "grant"} data-testid="recovery-grant">
            {busy === "grant" ? "Granting…" : "Grant exactly this request"}
          </button>
          <button type="button" className="button secondary" onClick={() => void decide("deny")} disabled={busy !== null} aria-busy={busy === "deny"} data-testid="recovery-deny">
            {busy === "deny" ? "Denying…" : "Deny"}
          </button>
        </div>
      ) : approval.state === "granted" ? (
        <p className="muted">Granted {formatDate(approval.decided_at)}: waiting for the agent to resume it (used once; a changed store, backup or target refuses it).</p>
      ) : null}
    </article>
  );
}

function QuarantineRow({ record }: { record: QuarantineView }) {
  const [shown, setShown] = useState(false);
  const one = useQuery<MaintenanceInspect>(shown ? `/maintenance/inspect?quarantine_id=${record.id}` : null);
  return (
    <li data-testid="recovery-quarantined" data-quarantine-id={record.id} data-integrity={record.damaged_integrity}>
      <p>
        <strong>{record.damaged_store_label}</strong> · <StateBadge state="quarantined" /> · integrity{" "}
        {record.damaged_integrity} · replaced by {record.replacement_store_label} on {formatDate(record.switched_at)} (
        {record.switched_by})
      </p>
      <p className="muted">
        Kept unchanged for inspection. Database digest <code>{short(record.damaged_db_sha256)}</code>; reason: {record.reason}
      </p>
      {shown ? (
        <QueryView query={one} loadingLabel="Inspecting the quarantined store">
          {(data) => (
            <p data-testid="recovery-quarantine-health">
              {HEALTH_TEXT[data.health] ?? data.health} Digest now <code>{short(data.database.sha256)}</code>.
            </p>
          )}
        </QueryView>
      ) : (
        <button type="button" className="link-button" onClick={() => setShown(true)} data-testid="recovery-quarantine-inspect">
          Inspect this store
        </button>
      )}
    </li>
  );
}

function OwnerActions({
  data,
  onDone,
  onRestored,
}: {
  data: MaintenanceInspect;
  onDone: () => void;
  onRestored: (r: MaintenanceRestored) => void;
}) {
  const { announce, maintenance } = useHub();
  const [busy, setBusy] = useState<"backup" | "migrate" | "journal" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const act = async (verb: "backup" | "migrate" | "journal") => {
    setBusy(verb);
    setError(null);
    try {
      if (verb === "backup") await post("/maintenance/backups", {});
      else if (verb === "migrate") await post("/maintenance/migrate", {});
      else onRestored(await post<MaintenanceRestored>("/maintenance/journal-recovery", {}));
      announce(
        verb === "backup"
          ? "Verified backup written into the store's exports folder."
          : verb === "migrate"
            ? "Store migrated after a verified backup."
            : "The interrupted write was recovered into a new store.",
      );
      onDone();
    } catch (err) {
      setError(err);
      announce(describeError(err).title);
    } finally {
      setBusy(null);
    }
  };
  const canBackup = !maintenance && data.opens;
  const canMigrate = data.health === "needs_migration";
  const canRecoverJournal = data.health === "needs_journal_recovery";
  if (!canBackup && !canMigrate && !canRecoverJournal) return null;
  return (
    <section aria-labelledby="owner-actions-title" data-testid="recovery-owner-actions">
      <h2 id="owner-actions-title">Maintenance</h2>
      {error ? <ErrorState error={error} /> : null}
      <div className="button-row">
        {canBackup ? (
          <button type="button" className="button secondary" onClick={() => void act("backup")} disabled={busy !== null} aria-busy={busy === "backup"} data-testid="recovery-backup-create">
            {busy === "backup" ? "Backing up…" : "Create a verified backup"}
          </button>
        ) : null}
        {canMigrate ? (
          <button type="button" className="button primary" onClick={() => void act("migrate")} disabled={busy !== null} aria-busy={busy === "migrate"} data-testid="recovery-migrate">
            {busy === "migrate" ? "Migrating…" : "Migrate this store (a verified backup first)"}
          </button>
        ) : null}
        {canRecoverJournal ? (
          <button type="button" className="button primary" onClick={() => void act("journal")} disabled={busy !== null} aria-busy={busy === "journal"} data-testid="recovery-journal">
            {busy === "journal" ? "Recovering…" : "Recover the interrupted write into a new store"}
          </button>
        ) : null}
      </div>
    </section>
  );
}

interface DeviceView {
  device_id: string;
  label: string;
  state: "active" | "expired" | "revoked";
  created_at: string;
  last_used_at: string;
  expires_at: string;
  revoked_at: string | null;
}

// Remembered browsers (A07, CP21.2): a browser paired at the home's own address stays signed in
// across hub restarts until it is revoked, signs out, or goes 14 days unused (90 days at most).
function RememberedBrowsers() {
  const devices = useQuery<{ devices: DeviceView[] }>("/devices");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const revoke = async (deviceId: string) => {
    setBusy(deviceId);
    setError(null);
    try {
      await post("/devices/revoke", { device_id: deviceId });
      devices.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };
  return (
    <section aria-labelledby="devices-title" data-testid="recovery-devices">
      <h2 id="devices-title">Remembered browsers</h2>
      <p className="muted">
        A browser opened with <code>prometheos start</code> stays signed in after the hub restarts. Revoke one
        to sign it out at once; it pairs again with <code>prometheos start</code>.
      </p>
      {error ? <ErrorState error={error} /> : null}
      <QueryView query={devices} loadingLabel="Reading remembered browsers">
        {(data) => {
          const shown = data.devices.filter((d) => d.state === "active");
          return shown.length === 0 ? (
            <Empty>No browser is remembered.</Empty>
          ) : (
            <ul className="plain-list">
              {shown.map((d) => (
                <li key={d.device_id} data-device-id={d.device_id}>
                  {d.label} · last used {formatDate(d.last_used_at)} · until {formatDate(d.expires_at)}{" "}
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => void revoke(d.device_id)}
                    disabled={busy !== null}
                    aria-busy={busy === d.device_id}
                    data-testid="device-revoke"
                  >
                    {busy === d.device_id ? "Revoking…" : "Revoke"}
                  </button>
                </li>
              ))}
            </ul>
          );
        }}
      </QueryView>
    </section>
  );
}

export function RecoveryPage() {
  const { maintenance } = useHub();
  const [refreshCount, setRefreshCount] = useState(0);
  const inspect = useQuery<MaintenanceInspect>(
    refreshCount === 0 ? "/maintenance/inspect" : `/maintenance/inspect?refresh=true&request=${refreshCount}`,
  );
  const requests = useQuery<{ approvals: MaintenanceApprovalView[] }>("/maintenance/approvals");
  const [restored, setRestored] = useState<MaintenanceRestored | null>(null);
  const reload = inspect.reload;
  const previousRequests = useRef<string | null>(null);
  const poll = useCallback(() => requests.reload(), [requests.reload]);
  useEffect(() => {
    if (!requests.data) return;
    const signature = JSON.stringify(requests.data.approvals.map((a) => [a.approval_request_id, a.state]));
    if (previousRequests.current !== null && previousRequests.current !== signature) reload();
    previousRequests.current = signature;
  }, [requests.data, reload]);
  useEffect(() => {
    const timer = window.setInterval(poll, POLL_MS);
    return () => window.clearInterval(timer);
  }, [poll]);
  return (
    <>
      <PageHeading
        title="Recovery"
        lead={
          maintenance
            ? "This project's store cannot be opened. Nothing in it will be changed: inspect it, verify a backup, restore it into a new store and switch to that store. The damaged store stays listed for inspection."
            : "Inspect the project's store, verify a backup and restore it into a new store. The current store is never overwritten."
        }
      />
      <button type="button" className="button secondary" data-testid="recovery-refresh" disabled={inspect.loading} onClick={() => setRefreshCount((n) => n + 1)}>
        Refresh store health
      </button>
      <QueryView query={inspect} loadingLabel="Inspecting the store">
        {(data) => {
          const approvals = requests.data?.approvals ?? data.approvals;
          const open = approvals.filter((a) => a.state === "pending" || a.state === "granted");
          const ended = approvals.filter((a) => a.state !== "pending" && a.state !== "granted").slice(-10).reverse();
          return (
            <>
              {maintenance && data.opens ? (
                <p className="inline-warning" role="status" data-testid="recovery-restart">
                  The project's store now opens. Restart the hub (<code>prometheos hub serve</code>) and any MCP server in
                  maintenance mode to work in the project again.
                </p>
              ) : null}
              <Health data={data} />
              <OwnerActions data={data} onDone={reload} onRestored={setRestored} />
              <section aria-labelledby="requests-title" data-testid="recovery-requests">
                <h2 id="requests-title">Requests from agents ({open.length})</h2>
                {open.length === 0 ? (
                  <Empty>No pending maintenance requests.</Empty>
                ) : (
                  open.map((a) => <RequestCard key={a.approval_request_id} approval={a} current={data} onDone={requests.reload} />)
                )}
                {ended.length > 0 ? (
                  <ul className="plain-list" data-testid="recovery-request-history">
                    {ended.map((a) => (
                      <li key={a.approval_request_id} data-approval-id={a.approval_request_id} data-state={a.state}>
                        <StateBadge state={a.state} /> {OPERATION_TEXT[a.operation] ?? a.operation} ·{" "}
                        {formatDate(a.closed_at ?? a.decided_at)}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </section>
              <section aria-labelledby="backups-title" data-testid="recovery-backups">
                <h2 id="backups-title">Backups in the store's exports folder ({data.backups.length})</h2>
                {data.backups.length === 0 ? (
                  <Empty>No backup archive was found. An agent or the command line can name another archive.</Empty>
                ) : (
                  <ul className="plain-list">
                    {data.backups.map((b) => (
                      <BackupRow key={b.name} name={b.name} size={b.bytes} modified={b.modified_at} onRestored={setRestored} />
                    ))}
                  </ul>
                )}
              </section>
              {restored ? <Restored restored={restored} health={data.health} onSwitched={reload} /> : null}
              <section aria-labelledby="quarantine-title" data-testid="recovery-quarantine">
                <h2 id="quarantine-title">Quarantined stores ({data.quarantine.length})</h2>
                {data.quarantine.length === 0 ? (
                  <Empty>No store has been replaced.</Empty>
                ) : (
                  <ul className="plain-list">
                    {data.quarantine.map((r) => (
                      <QuarantineRow key={r.id} record={r} />
                    ))}
                  </ul>
                )}
              </section>
            </>
          );
        }}
      </QueryView>
      {maintenance ? null : <RememberedBrowsers />}
    </>
  );
}
