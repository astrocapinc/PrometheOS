// Shapes returned by the hub API (see work11 contracts/schemas and domain/bodies.py).
// Only the fields the UI reads are typed; everything else stays opaque.

export interface RecordRef {
  id: string;
  rev: number | null;
}

export interface TextLocator {
  type: string;
  start_line?: number;
  end_line?: number;
  [key: string]: unknown;
}

export interface EvidenceRef {
  source_id: string;
  version_id: string;
  locator: TextLocator;
  quote_sha256: string | null;
}

export interface Envelope<B = Record<string, unknown>> {
  id: string;
  kind: string;
  rev: number;
  lifecycle: string;
  sensitivity: string;
  occurred_at: string | null;
  recorded_at: string;
  body: B;
}

export interface RecordList<B = Record<string, unknown>> {
  items: Envelope<B>[];
  next_cursor: string | null;
  project_seq: number;
}

export interface ProjectInfo {
  project_id: string;
  project_seq: number;
  record_counts: Record<string, number>;
  settings: { title: string; current_objective: string; timezone?: string | null };
  settings_rev: number;
}

export interface Session {
  paired: boolean;
  csrf_token?: string;
  project_id?: string;
  // A02, CP10.5: "maintenance" when the project store does not open (recovery screen only).
  mode?: "normal" | "maintenance";
}

export interface NextCandidate {
  id: string;
  kind: string;
  why_now: string;
  would_change: string;
  expected_output: string;
  effort: string;
  adopted_action_id: string | null;
  source_refs: string[];
  dependencies: string[];
}

export interface NextSteps {
  candidates: NextCandidate[];
  generation: string;
  project_seq: number;
}

export interface NotebookSource {
  availability: string;
  locator: TextLocator;
  /** Dogfood F5: a receipt citation in words ("output model: Plant model"), else null. */
  locator_label?: string | null;
  artifact?: { direction: string; role: string; title: string; source_id: string; version_id: string } | null;
  source_id: string;
  title: string;
  version_id: string;
}

export interface NotebookEntry {
  id: string;
  rev: number;
  title: string;
  text: string;
  category: string;
  origin: string;
  review: string;
  occurred_at: string | null;
  recorded_seq: number;
  sources: NotebookSource[];
  claims: unknown[];
  quantities: unknown[];
  correction_notice: unknown;
  needs_review: unknown;
}

export interface Notebook {
  entries: NotebookEntry[];
  project_seq: number;
}

export interface ClaimBody {
  statement: string;
  scope: {
    system: string;
    operating_regime: string;
    comparator: string;
    model_fidelity: string;
    validation_level: string;
    /** A02, CP10.4: the research objective the finding answers; null for earlier claims. */
    objective?: string | null;
  };
  evidence: EvidenceRef[];
  state: string;
  superseded_by: RecordRef | null;
  /** A06.1, CP20.1: a one-line lead for a card; absent on earlier claims. */
  summary?: string | null;
}

export interface CorrectionEffect {
  target_ref: RecordRef;
  effect: string;
  replacement_ref: RecordRef | null;
  replacement_text: string | null;
}

export interface CorrectionBody {
  target_refs: RecordRef[];
  replacement_refs: RecordRef[];
  reason: string;
  evidence: EvidenceRef[];
  effects: CorrectionEffect[];
  status: "proposed" | "applied" | "rejected";
  applied_seq: number | null;
}

export interface ItemBody {
  meeting_id: string | null;
  type: string;
  text: string;
  evidence: EvidenceRef[];
  proposed_owner: RecordRef | null;
  due_at: string | null;
  status: string;
  action_id: string | null;
}

export interface ActionBody {
  title: string;
  reason: string;
  source_refs: RecordRef[];
  owner_ref: RecordRef | null;
  due_at: string | null;
  dependencies: string[];
  status: string;
  blocker: string | null;
  completion_evidence: EvidenceRef[];
  commitment_item_id: string | null;
  // A05, CP18.1: work-item fields; absent on actions stored before A05.
  description?: string | null;
  labels?: string[];
}

export interface MeetingBody {
  title: string;
  held_at: string | null;
  source_version_refs: RecordRef[];
  processing: string;
}

export interface EntryBody {
  title: string;
  text: string;
  category: string;
  evidence: EvidenceRef[];
}

export interface SourceBody {
  title: string;
  origin: string;
  retention: string;
  description: string;
}

export interface ReconcileItem {
  item_id: string;
  type: string;
  text: string;
  status: string;
  owner: string;
  due: string;
  classification: string;
  evidence: EvidenceRef[];
  audio_spans: AudioSpan[];
  related_actions: RelatedAction[];
}

export interface RelatedAction {
  action_id: string;
  status: string;
  overlap: number;
  linked: boolean;
}

/** What an audio-locator citation says now (the cited transcript text and later corrections). */
export interface AudioSpan {
  version_id: string;
  start_ms: number;
  end_ms: number;
  clock: string;
  segment_ids: string[];
  quote: string;
  uncertain: boolean;
  newer_version_id: string | null;
  text_changed_since: boolean;
}

export interface Reconcile {
  meeting_id: string;
  held_at: string | null;
  items: ReconcileItem[];
  note: string;
  project_seq: number;
}

export interface VersionText {
  version_id: string;
  source_id: string;
  availability: string;
  text: string | null;
  reason: string | null;
  truncated: boolean;
  mime: string;
  byte_length: number;
}

export interface SearchHit {
  id: string;
  kind: string;
  title: string;
  excerpt: string;
  lifecycle: string;
}

export interface AssumptionRef {
  id: string;
  rev: number;
  current_rev: number;
  title: string;
  text: string;
  state: "active" | "superseded";
  superseded_by: RecordRef | null;
  stale: boolean;
}

export interface Links {
  id: string;
  incoming: { id: string; rev: number | null; via: string }[];
  outgoing: { id: string; rev: number | null; via: string }[];
  /** A02, CP10.4: the attributed assumptions a claim or entry relies on. */
  relies_on?: AssumptionRef[];
  /** For an assumption: what relies on it and the revision each was bound to. */
  relied_on_by?: { id: string; kind: string; rev: number; relied_on_rev: number; title: string }[];
}

export interface Changes {
  after_seq: number;
  latest_seq: number;
  changed: { id: string; kind: string; rev: number; seq: number }[];
  truncated?: boolean;
  reset?: boolean;
}

export interface ContextManifest {
  coverage: {
    changed_origins: unknown[];
    missing_versions: unknown[];
    reference_only_versions: unknown[];
    unavailable_roots: unknown[];
  };
  sections: { name: string; included: string[]; excluded_count: number; critical: boolean }[];
  project_seq: number;
}

export interface ContextPack {
  manifest: ContextManifest;
  text: string;
}

export interface DecisionPreview {
  digest: string;
  protected_effects: string[];
  root_changes: { alias: string; old_path: string | null; new_path: string | null }[];
  would_apply: boolean;
  operations: string[];
  expected_seq: number;
  error: unknown;
}

export interface CommitResult {
  ok: boolean;
  project_seq: number;
  replayed: boolean;
  request_id: string;
  results: { id: string; op_index: number; rev: number }[];
}

export interface DecisionApplied {
  preview: DecisionPreview;
  result: CommitResult;
}

export interface SkillSummary {
  name: string;
  version: string;
  status: string;
  package_digest: string;
  imported_at: string;
  trust: Record<string, boolean | string | null>;
}

export interface RecipeStep {
  id: string;
  instruction: string;
  requires: string[];
  expected_artifacts: string[];
  checks: string[];
  skippable: boolean;
}

export interface Recipe {
  id: string;
  title: string;
  steps: RecipeStep[];
  inputs: unknown;
  outputs: unknown;
}

export interface SkillDetail {
  package: string;
  package_digest: string;
  status: string;
  description: string;
  capabilities: string[];
  trust: Record<string, boolean | string | null>;
  origin: Record<string, unknown>;
  files: Record<string, { license: string | null; sha256: string; executable: boolean }>;
  recipes: Recipe[];
  license_complete: boolean;
  digests_ok: boolean;
  problems: unknown[];
  unknown_rights: unknown[];
  note: string;
}

export interface InstallPlan {
  plan: {
    host: string;
    package: string;
    plan_sha256: string;
    operations: { action: string; path: string; sha256: string }[];
    executable_files: unknown[];
    conflicts: unknown[];
    unknown_rights: unknown[];
    note: string;
    skills: { name: string; description: string }[];
  };
}

// ----- CP7: papers, anchors, readiness, jobs ---------------------------------------------

export interface Requirement {
  id: string;
  text: string;
  source_ref: EvidenceRef;
  applicability: "unknown" | "applicable" | "not_applicable";
  last_checked_at: string | null;
  status: "unknown" | "todo" | "met" | "not_applicable";
  evidence: EvidenceRef[];
}

export interface ReviewItem {
  id: string;
  comment_source_ref: EvidenceRef;
  text: string;
  response_text: string;
  change_refs: RecordRef[];
  status: "open" | "drafted" | "resolved";
  resolution_evidence: EvidenceRef[];
}

export interface ReviewRound {
  id: string;
  label: string;
  source_refs: RecordRef[];
  received_at: string | null;
  items: ReviewItem[];
}

export interface PaperBody {
  title: string;
  objective: string;
  claim_refs: RecordRef[];
  manuscript_source_id: string | null;
  venue_source_refs: RecordRef[];
  status: string;
  deadline: string | null;
  requirements: Requirement[];
  contribution: string;
  related_papers: { paper_id: string; relationship: string; explanation: string }[];
  review_rounds: ReviewRound[];
}

export interface AnchorBody {
  paper_id: string;
  manuscript_version_id: string;
  locator: TextLocator;
  claim_refs: RecordRef[];
  evidence: EvidenceRef[];
  status: "current" | "needs_review" | "resolved";
  reason: string | null;
}

export interface VersionBody {
  source_id: string;
  sha256: string;
  byte_length: number;
  mime: string;
  captured_at: string;
  availability: string;
}

export interface PaperOverview {
  project_seq: number;
  paper: Envelope<PaperBody>;
  anchors: Envelope<AnchorBody>[];
  manuscript_versions: Envelope<VersionBody>[];
  used_versions: Envelope<VersionBody>[];
}

export interface PaperFinding {
  code: string;
  record_ids: string[];
  detail: Record<string, unknown>;
}

// Decision 0111: where every number in the manuscript comes from.
export type NumberTrace = "run" | "run_inputs_changed" | "file_only";
export interface PaperNumberRow {
  kind: "typed" | "claimed";
  path: string | null;
  line: number;
  text: string;
  context: string;
  claim_id: string | null;
  claim_status: string | null;
}
export interface PaperNumberFile {
  path: string | null;
  version_id: string;
  sha256: string;
  role: "macros" | "generated";
  defined: number;
  used: number;
  trace: NumberTrace;
  runs: {
    receipt_version_id: string;
    receipt_title: string;
    command: string;
    outcome: string;
    ended_at: string | null;
    inputs_changed: { path: string | null; read_version_id: string; newer_version_id: string }[];
  }[];
}
export interface PaperNumbers {
  paper_id: string;
  headline: string;
  rule: string;
  limitations: string[];
  manuscript: { version_id: string; path: string | null; sha256: string } | null;
  counts: Record<string, number>;
  inputs_not_recorded: string[];
  inputs_skipped: string[];
  macro_files: PaperNumberFile[];
  numbers: PaperNumberRow[];
  truncated: boolean;
}

export interface PaperCheck {
  paper_id: string;
  paper_rev: number;
  project_seq: number;
  title: string;
  status: string;
  deadline: string | null;
  manuscript: {
    source_id: string | null;
    latest: null | {
      version_id: string;
      sha256: string;
      mime: string;
      captured_at: string;
      availability: string;
    };
  };
  venue: {
    known: boolean;
    sources: { version_id: string; source_id: string; captured_at: string; availability: string }[];
  };
  findings: PaperFinding[];
  counts: Record<string, number>;
  hidden_references: number;
  findings_digest: string;
  limitations: string[];
}

export interface InboxItem {
  id: number;
  job_id: string;
  due_utc: string;
  commit_seq: number;
  category: string;
  title: string;
  summary: string;
  record_ids: string[];
  created_at: string;
  dismissed_at: string | null;
}

export interface Inbox {
  project_seq: number;
  items: InboxItem[];
}

export interface JobListing {
  project_seq: number;
  jobs: {
    job: Envelope<{
      handler: string;
      enabled: boolean;
      last_slot: string | null;
      schedule: Record<string, unknown>;
    }>;
    recent_slots: { due_utc: string; state: string; reason: string | null }[];
    slot_counts: Record<string, number>;
    next_slots: { due_utc?: string; local?: string; skipped?: string }[];
  }[];
}


// ----- CP6: audio, recording, transcription, provider -----------------------------------------

export interface TranscriptSegment {
  id: string;
  start_ms: number;
  end_ms: number;
  text: string;
  speaker_label: string | null;
  confidence: number | null;
  uncertain: boolean;
}

export interface TranscriptView {
  version_id: string;
  source_id: string;
  sensitivity: string;
  payload: { schema_version: number; segments: TranscriptSegment[] };
  duration_ms: number;
  transformation: {
    type: string;
    engine: string;
    engine_version: string;
    model: string | null;
    language: string | null;
  } | null;
  audio_version_id: string | null;
  audio_available: boolean;
  latest_version_id: string | null;
  superseded_by_correction: boolean;
}

export interface VersionBody {
  source_id: string;
  mime: string;
  availability: string;
  byte_length: number;
  payload_schema: string | null;
}

export interface SourceBodyLite {
  title: string;
  origin: string;
}

export interface CaptureDevice {
  id: string;
  name: string;
  kind: "microphone" | "loopback";
  sample_rate: number;
  channels: number;
  default: boolean;
}

export interface RecordingSession {
  session_id: string;
  state: string;
  title: string;
  sensitivity: string;
  backend: string;
  sources: CaptureDevice[];
  elapsed_ms: number;
  level_dbfs: number;
  level_fraction: number;
  abandoned: boolean;
  recoverable: boolean;
  reason: string | null;
  version_id: string | null;
  consent_acknowledged: boolean;
  segments?: number;
}

export interface TranscriptionRun {
  run_id: string;
  version_id: string;
  state: string;
  engine: string;
  engine_version: string;
  model: string | null;
  chunks_done: number;
  chunk_count: number | null;
  cancel_requested: boolean;
  error: string | null;
  result_version_id: string | null;
}

export interface AudioModels {
  status: { models: { model: string; installed: boolean; location: string; total_bytes: number }[] };
  plan: {
    model: string;
    title: string;
    license: string;
    revision: string;
    total_mib: number;
    location: string;
    installed: boolean;
    plan_sha256: string;
  };
  synthetic_enabled: boolean;
}

export interface ProviderStatus {
  revision: number;
  config: {
    policy: string;
    endpoint: string | null;
    model: string | null;
    credential_ref: string | null;
    price_input_per_mtok_usd: string | null;
    price_output_per_mtok_usd: string | null;
    budget: {
      max_input_chars: number;
      max_output_tokens: number;
      max_calls_per_run: number;
      spend_cap_usd: string | null;
      timeout_seconds: number;
    };
  };
  policy: string;
  route: string;
  endpoint_host: string;
  capabilities: {
    capability: string;
    provider: string | null;
    route: string;
    available: boolean;
    note: string;
  }[];
  credential: { reference: string | null; present: boolean; store: string };
  usage: {
    calls: number;
    unknown_outcomes: number;
    spent_estimate_usd: string;
    cap_usd: string | null;
    remaining_usd: string | null;
    label: string;
  };
  digest: string;
}

export interface ProviderPreview {
  purpose: string;
  meeting_id: string | null;
  policy: string;
  route: string;
  endpoint_host: string;
  model: string | null;
  inputs: { records: number; sensitivity: string; restricted_included: boolean; excluded_above_scope: number | null };
  transmits: {
    characters: number;
    estimated_input_tokens: number;
    max_output_tokens: number;
    payload_sha256: string | null;
    excerpt: string;
    excerpt_truncated: boolean;
  };
  cost: { estimate_upper_bound_usd: string | null; label: string };
  calls: { max_per_run: number; note: string };
  credential: { reference: string | null; present: boolean; store: string };
  blockers: string[];
  unacknowledged_unknown_calls: string[];
  would_send: boolean;
  needs_egress_grant: boolean;
  digest: string;
  fallback: string;
}

export interface ProviderCall {
  call_id: string;
  purpose: string;
  route: string;
  endpoint_host: string;
  model: string;
  attempt: number;
  state: string;
  reserved_cost_usd: string | null;
  actual_cost_usd: string | null;
  created_at: string;
}

export interface ProviderConfigPreview {
  config: ProviderStatus["config"];
  route: string;
  policy: string;
  changes: string[];
  stale: boolean;
  digest: string;
  credential: { reference: string | null; present: boolean; store: string };
}

// A02, CP10.2: pending approvals (decision 0016). The grant is held by the server: no view
// carries its secret or capability ID.
export interface ApprovalView {
  approval_request_id: string;
  operation: string;
  state: "pending" | "granted" | "denied" | "withdrawn" | "expired" | "revoked" | "used";
  caller_rank: number;
  actor: string;
  request_id: string | null;
  plan: Record<string, unknown>;
  plan_sha256: string;
  required_scope: string;
  summary: string[];
  owner_route: { hub: string; cli: string };
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by: string | null;
  decision_note: string | null;
  used_at: string | null;
  used_seq: number | null;
  closed_at: string | null;
  result: Record<string, unknown> | null;
  // Owner rendering only: the transaction preview, or the owner's full view of a push or purge.
  preview?: Record<string, unknown> | null;
}


// A02, CP10.4: research continuity (decision 0018).
export interface NowItem {
  id: string;
  kind: string;
  title: string;
  reason: string;
  cause_id?: string | null;
  seq?: number;
  due_at?: string;
  status?: string;
  state?: string;
  since?: string;
  operation?: string;
  actor?: string;
  expires_at?: string;
  owner_route?: { hub: string; cli: string };
  /** A result an agent's assessment cleared without the owner's review (decisions 0018, 0078). */
  checked_by_agent?: boolean;
  outcomes?: (string | null)[];
  /** Results to interpret only (decision 0110, L2): a draft reading per bound quantity. */
  drafts?: ResultDraft[];
}

/** Decision 0110 (L2): what a rerun's output says about a claim's bound quantity, read by code. */
export interface ResultDraft {
  claim_id: string;
  claim_line: string;
  quantity_index: number;
  name?: string | null;
  unit: string;
  status: string;
  found_by: "label" | "layer" | null;
  expected: { decimal: string; version_id: string; source_pointer: string };
  measured: { decimal: string; source_id: string; version_id: string; line: number; source_pointer: string } | null;
  difference: string | null;
  relative_difference_percent: string | null;
  tolerance: null;
  layer: { agree: boolean; models: string[]; executors?: string[]; hint_only?: boolean } | null;
  layer_subject: string | null;
  summary: string;
}

export interface NowSection {
  count: number;
  items: NowItem[];
  truncated: boolean;
  /** "changed" only: how many of its items are agent-checked results (decision 0078). */
  checked_by_agent?: number | null;
}

export interface NowView {
  generation: string;
  project_seq: number;
  generated_at: string;
  since_seq: number | null;
  changed: NowSection;
  due: NowSection;
  running: NowSection;
  awaiting_interpretation: NowSection;
  approvals: NowSection;
  brief?: NowBrief;
}

// Decision 0109: the brief on Now, in a fixed, explained order (no model, no score).
export type BriefGroup = "decide" | "due" | "evidence" | "interpret";

export interface BriefItem {
  group: BriefGroup;
  kind: string;
  id: string;
  title: string;
  why: string;
  status?: string;
}

export interface NowBrief {
  generation: string;
  rule: string;
  items: BriefItem[];
  total: number;
  counts: Record<BriefGroup, number>;
}

export interface ResolveResult {
  preview: { protected_effects: string[] } | null;
  result: { project_seq: number; replayed?: boolean };
}

// A05, CP18.2-CP18.3: research events (decision 0035). Data holds no record text.
export interface ResearchEvent {
  event_id: string;
  type: string;
  subject_ref: { kind: string; id: string };
  subject_title?: string | null; // joined when read (researcher text); never in the event
  occurred_at: string;
  project_seq: number;
  data: Record<string, unknown> & { due_at?: string };
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  acknowledged_note: string | null;
  reason: string;
}

export interface EventList {
  generation: string;
  state: string;
  items: ResearchEvent[];
  next_cursor: string | null;
}

export interface AcknowledgeResult {
  acknowledged: ResearchEvent[];
}

export interface TickResult {
  events: { checked_at: string; emitted: { event_id: string; type: string }[] };
}

export interface CoverageItem {
  version_id: string;
  source_id: string;
  title: string;
  mime: string;
  byte_length: number;
  storage: "body" | "media" | "external";
  availability: "retained" | "reference_only" | "unavailable";
  availability_reason: string;
  origin_changed: boolean | null;
  backup: {
    covered: boolean;
    status: "backed_up" | "not_backed_up";
    reason: string;
    backups: { label: string; purpose: string; project_seq: number; created_at: string }[];
  };
}

export interface CoverageView {
  totals: {
    versions: number;
    retained: number;
    reference_only: number;
    unavailable: number;
    backed_up: number;
    not_backed_up: number;
  };
  verified_backups: number;
  items: CoverageItem[];
  next_cursor: string | null;
}

export interface CapabilityFeature {
  feature: string;
  description: string;
  available: boolean;
  blocker: { code: string; message: string } | null;
}

export interface CapabilitiesView {
  features: CapabilityFeature[];
}

export interface SourceCheckResult {
  observations: {
    version_id: string;
    origin_reachable: boolean;
    origin_matches_version: boolean | null;
    retained_digest_ok: boolean | null;
    error: string | null;
  }[];
  saved: boolean;
}

export interface ExecutionItem {
  id: string;
  title: string;
  host_label: string;
  reporter: string;
  state: string;
  effective_state: string;
  silent_since: string | null;
  last_reported_at: string | null;
  awaiting_interpretation: boolean;
  receipts: { version_id: string; title: string; outcome: string | null }[];
  interpreted_by: { id: string; kind: string; method?: string; review?: string }[];
}

export interface ExecutionList {
  executions: ExecutionItem[];
  truncated: boolean;
}

// A02, CP10.3 (decision 0017): the operation run view (contract operation_run_view).
export interface RunView {
  run_id: string;
  operation: string;
  state:
    | "queued"
    | "running"
    | "waiting_for_input"
    | "paused_budget"
    | "completed"
    | "failed"
    | "cancelled"
    | "unknown_external";
  stage: string | null;
  progress: { done: number; total: number | null; unit: string } | null;
  actor: string;
  caller_rank: number;
  params: Record<string, unknown>;
  params_digest: string;
  input_digests: { name: string; sha256: string }[];
  input_digest: string;
  config_digest: string;
  workflow_version: string | null;
  model: string | null;
  budget: { reservation: { unit: string; amount: string } | null; charged: string };
  external_effect: "none" | "started" | "confirmed" | "uncertain";
  external_call: {
    provider: string;
    external_ref: string | null;
    request_sha256: string | null;
    started_at: string;
  } | null;
  waiting_on:
    | { kind: "approval"; approval_request_id: string }
    | { kind: "input"; input_kind: string; description: string }
    | null;
  cancel_requested_at: string | null;
  cancel_confirmed: boolean;
  heartbeat_at: string | null;
  lease_expires_at: string | null;
  attempts: number;
  error: { code: string; message: string; retryable: boolean; details: Record<string, unknown> } | null;
  limitations: string[];
  artifacts: { id: string; rev: number; role: string }[];
  result_seq: number | null;
  result: Record<string, unknown> | null;
  resumable: boolean;
  job_id: string | null;
  job_due_utc: string | null;
  stages_completed: number;
  stages: { name: string; completed_at: string; charged: string | null }[];
  logs: { idx: number; at: string; level: "info" | "warning" | "error"; message: string }[];
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

// A02, CP10.5: the maintenance service and the recovery screen (decision 0021). Local paths
// never reach the browser: every store location has a label instead.
export interface MaintenanceApprovalView {
  approval_request_id: string;
  operation: "maintenance_switch" | "maintenance_migrate";
  state: "pending" | "granted" | "denied" | "withdrawn" | "expired" | "revoked" | "used";
  project_id: string;
  caller_rank: number;
  actor: string;
  request_id: string | null;
  plan: Record<string, unknown>;
  plan_sha256: string;
  summary: string[];
  owner_route: { hub: string; cli: string };
  damaged_store_label: string;
  damaged_db_sha256: string;
  backup_manifest_sha256: string | null;
  target_store_label: string | null;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by: string | null;
  grant_expires_at: string | null;
  used_at: string | null;
  closed_at: string | null;
}

export interface QuarantineView {
  id: string;
  project_id: string;
  damaged_store_label: string;
  damaged_db_sha256: string;
  damaged_schema_version: number | null;
  damaged_integrity: "ok" | "corrupt" | "unreadable" | "missing";
  replacement_store_label: string;
  backup_sha256: string;
  backup_manifest_sha256: string;
  maintenance_approval_id: string | null;
  switched_at: string;
  switched_by: string;
  reason: string;
}

export interface MaintenanceInspect {
  project_id: string;
  title: string | null;
  health: string;
  opens: boolean;
  store: { kind: string; quarantine_id: string | null; label: string; folder_present: boolean };
  database: {
    present: boolean;
    bytes: number | null;
    sha256: string;
    integrity: string;
    integrity_detail: string | null;
    schema_version: number | null;
    supported_schema_version: number;
    project_id: string | null;
    project_seq: number | null;
    journal: { wal: boolean; rollback_journal: boolean; state: "none" | "active" | "hot" };
    journal_recovery: { health: string; integrity: string; project_seq: number | null } | null;
  };
  lock: { maintenance: string };
  backups: { name: string; bytes: number; modified_at: string }[];
  approvals: MaintenanceApprovalView[];
  quarantine: QuarantineView[];
  environment: Record<string, unknown>;
  recovery: Record<string, string>;
}

export interface BackupVerified {
  verified: boolean;
  project_id: string;
  backup: { name: string; label: string; sha256: string; bytes: number };
  backup_manifest_sha256: string;
  manifest: {
    created_at: string;
    software_version: string;
    project_seq: number;
    schema_version: number;
    database_sha256: string;
    media_count: number;
    media_bytes: number;
    reference_only_count: number;
  };
}

export interface MaintenanceRestored {
  restored: boolean;
  registry_switched: boolean;
  project_id: string;
  target_store_label: string;
  target_managed: boolean;
  journal_recovered?: boolean;
  damaged_store_label: string;
  backup: { name: string; label: string; sha256: string; bytes: number };
  backup_manifest_sha256: string;
  manifest: BackupVerified["manifest"];
  next: string;
}

// ----- A05, CP17: adoption (decision 0039) ---------------------------------------------------

export interface AdoptionScanSummary {
  entries: number;
  files_on_disk: number;
  members: number;
  bytes_on_disk: number;
  by_status: Record<string, number>;
  by_container: Record<string, number>;
  by_zone: Record<string, number>;
  unreadable: number;
  limited: number;
  seconds: number;
}

export interface AdoptionScan {
  scan_gen: number;
  root_alias: string;
  run_id: string | null;
  state: "running" | "complete" | "failed";
  config: { exclusions: string[]; profile: string; previous_gen: number | null };
  started_at: string;
  finished_at: string | null;
  root_digest_before: string | null;
  root_digest_after: string | null;
  summary: AdoptionScanSummary | null;
  error: string | null;
  analysis: {
    evidence: number;
    by_predicate: Record<string, number>;
    extracted: number;
    replayed: number;
    // Analyzers this installation could not run (decision 0053), e.g. MATLAB parsing without the extra.
    analyzers_unavailable: { analyzer: string; reason: string; install: string; files: number }[];
  } | null;
}

export interface AdoptionScans {
  scans: AdoptionScan[];
  total: number;
}

export interface AdoptionFile {
  scan_gen: number;
  path: string;
  parent: string | null;
  depth: number;
  entry: "file" | "folder" | "link" | "member";
  size: number | null;
  mtime_us: number | null;
  sha256: string | null;
  text_sha256: string | null;
  container: string | null;
  status: string;
  reason: string | null;
  zone: string;
  change: string | null;
}

export interface AdoptionFiles {
  scan_gen: number;
  root_alias: string;
  state: string;
  total: number;
  offset: number;
  next_offset: number | null;
  files: AdoptionFile[];
}
export interface AdoptionEvidence {
  evidence_id: string;
  scan_gen: number;
  analyzer: string;
  analyzer_version: string;
  subject: string;
  subject_sha256: string | null;
  predicate: string;
  object: { path?: string | null; literal?: unknown; external?: string | null; facet?: string | null };
  locator: { kind: string; part?: string | null; start?: number | null; end?: number | null } | null;
  quote: string | null;
  quote_sha256: string | null;
  tier: string;
  determinism: string;
}

export interface AdoptionEvidenceList {
  scan_gen: number;
  root_alias: string;
  total: number;
  offset: number;
  next_offset: number | null;
  evidence: AdoptionEvidence[];
}
export interface AdoptionFamilySummary {
  family_id: string;
  key: string;
  kind: string;
  band: "decided" | "supported" | "weak" | "undecided";
  deciding_rule: string | null;
  current: string | null;
  members: number;
  conflicts: number;
  // A06.1, CP20.4 (decision 0085): the family's class and whether it needs the owner.
  class: AdoptionClass;
  class_reason: string;
  class_default: string | null;
  class_decision: "variant" | "noise" | "split" | null;
  needs_owner: boolean;
}

export type AdoptionClass =
  | "identical"
  | "tool_output"
  | "run_log"
  | "history"
  | "succession"
  | "variant"
  | "lineage"
  | "compete";

export interface AdoptionFamilyList {
  scan_gen: number;
  root_alias: string;
  total: number;
  offset: number;
  next_offset: number | null;
  families: AdoptionFamilySummary[];
}

export interface AdoptionFamily {
  scan_gen: number;
  family_id: string;
  key: string;
  kind: string;
  members: {
    path: string;
    outcome: "current" | "candidate" | "excluded" | "copy";
    copy_of: string | null;
    rank: number | null;
    reasons: { rule: string; tier: string; text: string; evidence: string[] }[];
  }[];
  current: string | null;
  band: "decided" | "supported" | "weak" | "undecided";
  deciding_rule: string | null;
  conflicts: { kind: string; text: string; evidence: string[]; paths: string[] }[];
  // A06.1, CP20.4 (decision 0085): the family's class and whether it needs the owner.
  class: AdoptionClass;
  class_reason: string;
  class_default: string | null;
  class_decision: "variant" | "noise" | "split" | null;
  // A06.1 under A07 (decision 0107): two agreeing models' proposal, shown beside the class.
  class_proposal: AdoptionClassProposal | null;
  needs_owner: boolean;
}

export interface AdoptionClassProposal {
  answer: "variant" | "succession" | "history" | "noise" | "compete";
  current: string | null;
  quote: { path: string; line: number; text: string } | null;
  models: string[];
}

// A06.1, CP20.4 (decision 0106): the scan's outline, proposed once and confirmed once.
export type OutlineKind = "track" | "deliverable" | "question" | "result" | "stale_candidate";

export interface OutlineItem {
  item_id: string;
  kind: OutlineKind;
  key: string;
  title: string;
  cites: { path: string; sha256: string | null; line: number | null; quote: string | null }[];
  source: "rule" | "proposed" | "rule+proposed";
  models: string[];
  files?: number;
  path?: string;
  producer?: string;
  reason?: string;
  addresses?: string | null;
  draws_on?: string[];
}

export interface AdoptionOutline {
  scan_gen: number;
  root_alias: string;
  version: number;
  items: OutlineItem[];
  counts: Record<OutlineKind, number>;
  truncated: boolean;
  sha256: string;
  confirmed: { outline_id: string; scan_gen: number; proposal_sha256: string; confirmed_at: string; confirmed_by: string } | null;
}

export interface OutlineEdits {
  drop: string[];
  titles: Record<string, string>;
  addresses: Record<string, string | null>;
  draws_on: Record<string, string[]>;
}

export interface OutlinePreview {
  scan_gen: number;
  proposal_sha256: string;
  kept: Record<OutlineKind, number>;
  dropped: number;
  records_created: number;
  records_reused: number;
  relations: number;
  unplaced_questions: number;
  objective_set: boolean;
}
export interface AdoptionAssistTasks {
  scan_gen: number;
  root_alias: string;
  total: number;
  tasks: { task_kind: string; subject: string; reason: string; samples_needed: number; done: boolean }[];
}

export interface AdoptionPlanStep {
  step_id: string;
  kind: string;
  actor: "owner" | "agent";
  title: string;
  why: string;
  family_ids: string[];
  inputs: { path: string; sha256: string | null }[];
  procedure: string;
  allowed: string[];
  forbidden: string[];
  output_schema: string;
  acceptance: string[];
  report_via: string;
}

export interface AdoptionPlan {
  plan_id: string;
  version: number;
  omitted_decisions: number;
  scan_gen: number;
  root_alias: string;
  steps: {
    step: AdoptionPlanStep;
    state: "ready" | "reported" | "validated" | "rejected" | "accepted" | "settled" | "superseded";
    action_id: string | null;
    plan_version: number;
    result: { problems?: string[]; outcome?: Record<string, unknown> } | null;
  }[];
}

export interface AdoptionBaselineAccepted {
  baseline_id: string;
  sha256: string;
  scan_gen: number;
  plan: { plan_id: string; version: number };
  accepted_steps: number;
}

export interface AdoptionDrift {
  scan_gen: number;
  root_alias: string;
  against: { kind: "previous" | "baseline"; scan_gen: number | null; baseline_id: string | null };
  counts: Record<"added" | "changed" | "moved" | "deleted", number>;
  analysis: {
    extracted: number;
    replayed: number;
    analyzers_unavailable: { analyzer: string; reason: string; install: string; files: number }[];
  };
  total: number;
  offset: number;
  next_offset: number | null;
  entries: {
    change: "added" | "changed" | "moved" | "deleted";
    path: string;
    from: string | null;
    sha256_before: string | null;
    sha256_after: string | null;
    status_after: string | null;
  }[];
  families: { family_id: string; key: string; current: string | null; change: string; now: string | null }[];
}

export interface AdoptionBaselines {
  baselines: { baseline_id: string; root_alias: string; scan_gen: number; plan_version: number; accepted_at: string; sha256: string }[];
  total: number;
}

// ----- the literature library (A02, CP11 S11.1; decision 0060) -----------------------------

export interface LibraryLocator {
  type: "pdf";
  page: number;
  bbox: null;
  start: number;
  end: number;
}

export interface LibraryEvidence {
  source_id: string;
  version_id: string;
  locator: LibraryLocator;
  quote_sha256: string;
}

export interface LibraryWorkSummary {
  work_id: string;
  rev: number;
  title: string;
  contributors: string[];
  year: number | null;
  venue: string | null;
  identifiers: Record<string, string>;
  access_state: "metadata_only" | "abstract_only" | "full_text" | "unavailable";
  page_count: number | null;
  pages_without_text: number;
  text_version_id: string | null;
  pdf_version_id: string | null;
  pdf_source_id: string | null;
  file: { alias: string; path: string } | null;
  sources: number;
  merged_into: string | null;
  sensitivity: string;
}

export interface LibraryWorks {
  project_seq: number;
  total: number;
  offset: number;
  next_offset: number | null;
  works: LibraryWorkSummary[];
}

export interface LibraryHit {
  work_id: string;
  title: string;
  text_version_id: string;
  pdf_version_id: string;
  page: number;
  start: number;
  end: number;
  snippet: { before: string; match: string; after: string };
  locator: LibraryLocator;
  quote_sha256: string;
  evidence: LibraryEvidence;
}

export interface LibrarySearch {
  mode: string;
  query: string;
  hits: LibraryHit[];
  content_trust: "untrusted_data";
  notice: string;
}

export interface LibraryRead {
  work_id: string | null;
  title: string | null;
  page_count: number;
  page: number;
  state: "text" | "no_text" | "too_long";
  page_chars: number;
  start: number;
  end: number;
  clamped: boolean;
  text: string;
  next: { page: number; start: number } | null;
  note: string | null;
  locator: LibraryLocator | null;
  quote_sha256: string | null;
  evidence: LibraryEvidence | null;
  notice: string;
}

export interface LibraryPlanEntry {
  key: string;
  lines: [number, number];
  action: "create" | "merge" | "update" | "unchanged" | "refused";
  work_id: string | null;
  title: string | null;
  identifiers: Record<string, string>;
  related: Record<string, string>;
  refused_fields: { field: string; value: string; reason: string }[];
  reason?: string;
}

export interface LibraryPlanItem {
  path: string;
  kind?: "bibliography";
  format?: "bibtex" | "ris" | "csv";
  action: "create" | "new_version" | "extract" | "unchanged" | "duplicate";
  sha256: string;
  byte_length: number;
  page_count?: number;
  pages_without_text?: number[];
  title?: string;
  title_basis?: string;
  contributors?: string[];
  doi?: string | null;
  arxiv?: string | null;
  work_id?: string;
  duplicate_of?: string;
  entries?: LibraryPlanEntry[];
  entry_counts?: Record<string, number>;
  unmapped_columns?: string[];
}

export interface LibraryRefusal {
  path: string;
  reason: string;
  detail: string;
}

export interface LibraryPlan {
  plan_id: string;
  root_alias: string;
  path: string;
  items: LibraryPlanItem[];
  refused: LibraryRefusal[];
  counts: Record<string, number>;
  more: number;
  expires_at: string;
}

export interface LibraryCommit {
  plan_id: string;
  outcomes: { path: string; outcome: string; reason?: string; detail?: string; work_id?: string }[];
  refused: LibraryRefusal[];
  counts: Record<string, number>;
}

export interface LibraryCandidateWork {
  work_id: string;
  rev: number;
  title: string;
  first_author: string | null;
  year: number | null;
  venue: string | null;
  identifiers: Record<string, string>;
}

export interface LibraryCandidate {
  candidate_id: string;
  kind: "duplicate" | "version";
  title_key: string;
  evidence: { same_title_key: string; first_author_family: string; years: (number | null)[] };
  works: LibraryCandidateWork[];
  suggested: string;
}

export interface LibraryDedup {
  candidates: LibraryCandidate[];
  total: number;
  decisions: { decision_id: string; work_a: string; work_b: string; choice: string; note: string | null; decided_by: string; decided_at: string }[];
  links: { preprint: string; preprint_title: string; published: string; published_title: string; origin: "import" | "decision" }[];
  notice: string;
}

export interface LibraryExport {
  format: "bibtex" | "ris" | "csv";
  count: number;
  works: { work_id: string; key: string }[];
  content: string;
}

export interface LibraryAnnotation {
  entry_id: string;
  rev: number;
  title: string;
  text: string;
  page: number;
  start: number | null;
  end: number | null;
  quote_sha256: string | null;
  recorded_at: string;
}

// ----- A02, CP11 S11.4: evidence matrices (decision 0064) -----------------------------------------

export type MatrixCellState = "proposed" | "reviewed" | "unsupported";
export type MatrixBasis = "full_text" | "abstract" | "metadata";

export interface MatrixColumn {
  id: string;
  label: string;
  value_kind: "text" | "quantity" | "choice";
  instructions: string;
  choices?: string[] | null;
}

export interface MatrixEvidence {
  source_id: string;
  version_id: string;
  locator: Record<string, unknown>;
  quote_sha256: string | null;
  where: {
    work_id: string;
    page?: number | null;
    start?: number | null;
    end?: number | null;
    start_line?: number | null;
    end_line?: number | null;
  };
}

export interface MatrixCell {
  column_id: string;
  value_text: string;
  quantities: { decimal: string; unit: string; source_pointer: string }[];
  evidence: MatrixEvidence[];
  basis: MatrixBasis;
  state: MatrixCellState;
  method: "manual" | "rule" | "model";
  rule: { engine: string; version: string } | null;
}

export interface MatrixFlag {
  reason: string;
  cause_id: string;
  created_seq: number;
}

export interface MatrixRow {
  work_id: string;
  work_title: string;
  work_rev: number;
  access_state: "metadata_only" | "abstract_only" | "full_text" | "unavailable";
  in_matrix: boolean;
  extraction_id: string | null;
  rev: number | null;
  sensitivity: string | null;
  rubric_version: number | null;
  stale: boolean;
  needs_review: boolean;
  flags: MatrixFlag[];
  cells: MatrixCell[];
}

export interface MatrixBrief {
  version_id: string;
  source_id: string;
  sha256: string;
  created_seq: number;
  recorded_at: string;
  needs_review: boolean;
  flags: MatrixFlag[];
}

export interface MatrixCounts {
  rows: number;
  works: number;
  cells: Record<MatrixCellState, number>;
  access_states: Record<string, number>;
  needs_review: number;
  stale: number;
}

export interface MatrixView {
  project_seq: number;
  matrix: {
    matrix_id: string;
    rev: number;
    title: string;
    columns: MatrixColumn[];
    rubric_version: number;
    sensitivity: string;
  };
  rows: MatrixRow[];
  briefs: MatrixBrief[];
  counts: MatrixCounts;
  notice: string;
}

export interface MatrixSummary {
  matrix_id: string;
  rev: number;
  title: string;
  rubric_version: number;
  columns: number;
  counts: MatrixCounts;
  latest_brief: string | null;
  brief_needs_review: boolean;
}

export interface MatrixList {
  project_seq: number;
  matrices: MatrixSummary[];
  notice: string;
}

export interface MatrixExport {
  project_seq: number;
  matrix_id: string;
  format: "csv" | "markdown" | "json";
  content: string;
  content_sha256: string;
  brief: { version_id: string; source_id: string; sha256: string; built_at_seq: number | null } | null;
}

// ----- A02, CP11 S11.5: the read-only Zotero adapter (decision 0070) ---------------------------

export interface ZoteroCollection {
  key: string;
  name: string;
  parent: string | null;
}

export interface ZoteroStatus {
  state: "available" | "not_running" | "local_api_disabled" | "unexpected";
  text: string;
  detail?: string;
  version: string | null;
  endpoint: string | null;
  collections?: ZoteroCollection[];
  collections_error?: string;
}

export interface ZoteroPlanItem {
  key: string;
  version: number;
  item_type: string;
  title: string | null;
  action: "create" | "merge" | "update" | "unchanged" | "refused";
  identifiers: Record<string, string>;
  related: Record<string, string>;
  has_abstract: boolean;
  work_id?: string | null;
  reason?: string;
  refused_fields: { field: string; value: string; reason: string }[];
}

export interface ZoteroPlan {
  plan_id: string;
  scope: { kind: "library" | "collection" | "items"; key?: string; keys?: string[] };
  items: ZoteroPlanItem[];
  refused: { key: string | null; reason: string; detail: string }[];
  skipped: Record<string, number>;
  counts: Record<string, number>;
  total: number;
  more: number;
  expires_at: string;
}

export interface ZoteroCommit {
  plan_id: string;
  outcomes: { key: string; outcome: string; action?: string; reason?: string; detail?: string }[];
  counts: Record<string, number>;
}

// A06.1, CP20.1 (decision 0082): a claim's derived status. A view of the record, never support.
export type ClaimStatusName =
  | "withdrawn"
  | "superseded"
  | "stale"
  | "conflicting"
  | "settling"
  | "unverified"
  | "corrected"
  | "holds";

export interface ClaimStatusItem {
  claim_id: string;
  rev: number;
  status: ClaimStatusName;
  basis: "reviewed" | "deterministic" | "agent_reported" | null;
  usable: boolean;
  summary: string | null;
  causes: { code: string; message: string; ref: string | null }[];
}

export interface ClaimStatusView {
  generation: string;
  project_seq: number;
  counts: Record<ClaimStatusName, number>;
  claims: ClaimStatusItem[];
}

// A06.1, CP20.2 (decision 0083): the research map. Derived; it changes nothing.
export interface MapEvidence {
  version_id: string;
  title: string | null;
  root_alias: string | null;
  path: string | null;
  sha256: string | null;
  byte_length: number | null;
  availability: string | null;
  // CP20.4 (gap audit #16): where the claim cites it (a whole file is {type: "file"}; a version
  // cited only by relation has none) and the recorded runs that wrote it.
  locator: { type: string; start_line?: number; end_line?: number; page?: number; pointer?: string } | null;
  runs: MapRun[];
}

export interface MapFile {
  version_id: string;
  title: string | null;
  root_alias: string | null;
  path: string | null;
  sha256: string | null;
}

export interface MapRun {
  receipt_version_id: string;
  receipt_title: string | null;
  reporter: string;
  command: string;
  outcome: "succeeded" | "failed" | "unknown";
  ended_at: string | null;
  producers: MapFile[];
}

export interface MapAssessment {
  assessment_id: string;
  outcome: "supports" | "challenges" | "inconclusive";
  method: string;
  basis: "reviewed" | "deterministic" | "agent_reported";
  checks: { name: string; result: "pass" | "fail" | "unknown"; expected: string | null; measured: string | null; tolerance: string | null }[];
}

export type LinkedBy = "agent" | "owner_or_cli" | null;

export interface MapClaim {
  claim_id: string;
  rev: number;
  line: string;
  status: ClaimStatusName;
  basis: ClaimStatusItem["basis"];
  usable: boolean;
  causes: ClaimStatusItem["causes"];
  reason: string;
  tracks: string[];
  tracks_by_path?: string[];
  replaces: string | null;
  linked_by: LinkedBy;
  details: { statement: string; state: string; evidence: MapEvidence[]; assessment: MapAssessment | null };
}

export interface MapQuestion {
  entry_id: string;
  rev: number;
  title: string;
  claims: MapClaim[];
  tracks: string[];
  linked_by: LinkedBy;
}

// Decision 0109 (L1): the layer's proposal for a claim that answers no question.
export interface MapSuggestion {
  agree: boolean;
  models: string[];
  question_id?: string;
  question_rev?: number | null;
  question_title?: string | null;
  hint_only?: boolean;
  quote?: string;
  executors?: string[];
}

export type MapUnplacedClaim = MapClaim & { suggestion: MapSuggestion | null };

export type StageKey = "framed" | "evidence_mapped" | "verified";

export interface MapBlocker {
  code: string;
  message: string;
  claim?: string;
  question?: string;
  line?: string;
}

export interface MapDeliverable {
  paper_id: string;
  rev: number;
  title: string;
  paper_status: PaperBody["status"];
  stage: StageKey | null;
  steps: { key: StageKey; label: string; reached: boolean; blockers: MapBlocker[] }[];
  next: StageKey | null;
  headline: string;
  questions: MapQuestion[];
  other_claims: MapClaim[];
  to_confirm: { claim_id: string; line: string }[];
  health: Record<ClaimStatusName, number>;
}

// A06.1, CP20.3 (decision 0084): planned checks, the budget and decision cards.
export interface PlannedCheck {
  action_id: string;
  rev: number;
  title: string;
  status: string;
  kind: "read_only" | "rerun" | "model_edit" | "literature";
  estimated_minutes: number | null;
  claim_id: string | null;
  claim_line: string | null;
  cause: string | null;
  // CP20.4 (gap audit #10): the claim no longer has the cause this check was made for.
  resolved: boolean;
  adoption_step: string | null;
  what: string | null;
  eligible: boolean;
  eligibility: string;
  why: string;
  planned_by: "prometheos" | "agent_or_owner";
}

export interface VerificationPlanView {
  generation: string;
  project_seq: number;
  budget: {
    max_minutes_each: number;
    max_minutes_per_day: number;
    output_root_alias: string | null;
    output_path: string | null;
    used_today: number;
    remaining_today: number;
    timezone: string;
    default: boolean;
  };
  checks: PlannedCheck[];
}

export interface DecisionCard {
  approval_request_id: string;
  plan_sha256: string;
  card: { what: string; why: string; recommendation: string; default_text: string; default_at?: string | null };
  protected_effects: string[];
  requested_by: string;
  created_at: string;
  expires_at: string;
  claims: { claim_id: string; line: string }[];
  default_state: "waiting" | "default_in_effect";
  plan_current: boolean;
  generated: boolean;
  /** Decision 0110 (L3): the layer's plain wording, numbers filled by PrometheOS; a hint. */
  wording?: { what: string; why: string; recommendation: string; models: string[]; executors: string[]; hint_only: boolean } | null;
}

export interface DecisionCardsView {
  generation: string;
  project_seq: number;
  cards: DecisionCard[];
  recommended: string[];
}

export interface RecommendedPreview {
  approval_request_ids: string[];
  plan_sha256s: string[];
  operations: number;
  preview: { digest: string; would_apply: boolean; protected_effects: string[] };
}

export interface ResearchMapView {
  generation: string;
  project_seq: number;
  objective: { text: string | null; entry_id: string | null; questions: MapQuestion[] };
  deliverables: MapDeliverable[];
  unplaced_questions: MapQuestion[];
  tracks: {
    entry_id: string;
    title: string;
    claims: number;
    questions: number;
    claims_by_path?: number;
    folders?: string[];
  }[];
  unplaced_claims?: MapUnplacedClaim[];
}

// ----- the intelligence layer (A07, CP22.4; decision 0094) ------------------------------------

export interface AiPolicyState {
  policy: {
    revision: number;
    policy: Record<string, unknown>;
    policy_sha256: string;
    provider_revision: number;
    granted_by: string;
    granted_at: string;
  } | null;
  in_force: boolean;
  void_reason: string | null;
  provider_revision: number;
  provider_is_openrouter: boolean;
  key: { reference: string | null; present: boolean; store?: string };
  spend: {
    month: string;
    monthly_cap: string | null;
    used: string;
    billed: string;
    estimated: string;
    held_calls: number;
  };
  tasks: string[];
}

export interface RootSensitivities {
  roots: { root_alias: string; sensitivity: string; set_by_owner: boolean; note: string | null }[];
}

export interface AiResults {
  results: {
    result_key: string;
    task_kind: string;
    task_version: number;
    subject: string;
    sample: number;
    executor: string;
    outcome: string;
    reasons: string[];
    hint_only: boolean;
    model: string;
    served_model: string | null;
    run_id: string;
    scan_gen: number | null;
    input_sha256: string;
    summary: string | null;
    claim: string | null;
  }[];
}

export interface AiEvaluations {
  evaluations: {
    evaluation_id: string;
    task_kind: string;
    task_version: number;
    model: string;
    providers: string[];
    executor: string;
    trusted: boolean;
    coverage: number;
    precision_lower_bound: number;
    correct: number;
    answered: number;
    items: number;
    why: string | null;
    measured_at: string;
  }[];
}
