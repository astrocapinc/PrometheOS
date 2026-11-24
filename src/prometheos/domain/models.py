"""Common public contracts: envelopes, references, request protocol v1 and responses.

Every public model rejects unknown fields, non-finite numbers and unencodable
strings. Raw JSON must enter through :func:`parse_model`, which also rejects
duplicate object keys (Pydantic's own JSON parser keeps the last duplicate).
Record bodies are JSON objects here; per-kind body models are validated by the
application layer because a ``revise`` body's type depends on the stored kind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from prometheos.domain.canonical import (
    CanonicalJSONError,
    JSONDepthError,
    check_input_depth,
    dumps_canonical,
    loads_strict,
)
from prometheos.domain.ids import UUID4_PATTERN, Kind, ProjectId, RecordId, RequestId, kind_of

MAX_TITLE = 300
MAX_TEXT = 65536
MAX_REFS = 200
MAX_OPERATIONS = 200
MAX_REQUEST_BYTES = 8 * 1024 * 1024
REQUEST_SCHEMA_VERSION = 1
MAX_INT = 2**53 - 1
"""Largest integer exchanged in public JSON (exact in IEEE doubles for browser clients)."""


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )


def parse_model[M: BaseModel](model: type[M], raw: bytes | str) -> M:
    """The single entry point for untrusted JSON: strict parse, then strict model."""
    loads_strict(raw)
    return model.model_validate_json(raw, strict=True)


def _bounded_ints(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int) and abs(value) > MAX_INT:
        raise ValueError(f"integer exceeds +/-{MAX_INT}")
    if isinstance(value, dict):
        for item in value.values():
            _bounded_ints(item)
    elif isinstance(value, list):
        for item in value:
            _bounded_ints(item)


def _encodable_json(value: dict[str, Any]) -> dict[str, Any]:
    try:
        dumps_canonical(value)
    except CanonicalJSONError as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc
    _bounded_ints(value)
    return value


JsonObject = Annotated[dict[str, JsonValue], AfterValidator(_encodable_json)]
"""A JSON object the application stores, wraps or returns, whose nested numbers are finite
and strings valid Unicode: held to the internal depth ceiling."""


def _admissible_json(value: dict[str, Any]) -> dict[str, Any]:
    check_input_depth(value)
    return _encodable_json(value)


JsonObjectInput = Annotated[dict[str, JsonValue], AfterValidator(_admissible_json)]
"""A JSON object a caller supplies as a parameter: held to the (lower) admission depth, so
that what is admitted can still be wrapped by the application's own envelopes."""


_TS_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"


def _valid_timestamp(value: str) -> str:
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value}") from exc
    return value


Timestamp = Annotated[str, StringConstraints(pattern=_TS_PATTERN), AfterValidator(_valid_timestamp)]
"""UTC RFC3339 timestamp with a literal ``Z`` suffix."""

Title = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TITLE)]
Text = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Sensitivity = Literal["normal", "restricted", "private"]
Lifecycle = Literal["active", "archived", "deleted"]
SENSITIVITY_ORDER: dict[str, int] = {"normal": 0, "restricted": 1, "private": 2}


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RecordRef(StrictModel):
    id: RecordId
    rev: int = Field(ge=1, le=MAX_INT)


# ----- evidence locators -------------------------------------------------------


class TextLocator(StrictModel):
    type: Literal["text"]
    start_line: int = Field(ge=1, le=MAX_INT)
    end_line: int = Field(ge=1, le=MAX_INT)

    @model_validator(mode="after")
    def _ordered(self) -> TextLocator:
        if self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


def _absent(value: Any) -> bool:
    return value is None


class PdfLocator(StrictModel):
    """A page and an optional box. CP11 S11.1 (decision 0060) adds optional ``start``/``end``
    character offsets into the page's extracted text (Unicode code points, end exclusive), so
    a quotation is checkable exactly. Additive: a locator without offsets serialises exactly
    as before (no null members), and older records stay valid."""

    type: Literal["pdf"]
    page: int = Field(ge=1, le=MAX_INT)
    bbox: tuple[float, float, float, float] | None
    start: int | None = Field(
        default=None,
        ge=0,
        le=MAX_INT,
        exclude_if=_absent,
        description="First character in the page's extracted text, in Unicode code points",
    )
    end: int | None = Field(
        default=None,
        ge=1,
        le=MAX_INT,
        exclude_if=_absent,
        description="End (exclusive) in the page's extracted text, in Unicode code points",
    )

    @model_validator(mode="after")
    def _offsets(self) -> PdfLocator:
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end are given together")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class AudioLocator(StrictModel):
    type: Literal["audio"]
    start_ms: int = Field(ge=0, le=MAX_INT)
    end_ms: int = Field(ge=0, le=MAX_INT)

    @model_validator(mode="after")
    def _ordered(self) -> AudioLocator:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be >= start_ms")
        return self


class JsonLocator(StrictModel):
    type: Literal["json"]
    pointer: Annotated[str, StringConstraints(max_length=1024, pattern=r"^(/([^~]|~[01])*)*$")]


class FileLocator(StrictModel):
    """A whole artefact, bound by its version digest (A06.1, CP20.1): for binary artefacts such
    as ``.mat``, ``.slx`` or ``.png`` that have no lines, pages or pointers. It carries no quote
    and binds no quantity."""

    type: Literal["file"]


Locator = Annotated[
    TextLocator | PdfLocator | AudioLocator | JsonLocator | FileLocator,
    Field(discriminator="type"),
]


class EvidenceRef(StrictModel):
    source_id: RecordId
    version_id: RecordId
    locator: Locator
    quote_sha256: Sha256 | None

    @model_validator(mode="after")
    def _kinds(self) -> EvidenceRef:
        if kind_of(self.source_id) != "source" or kind_of(self.version_id) != "version":
            raise ValueError("evidence requires a source_id and version_id")
        if self.locator.type == "file" and self.quote_sha256 is not None:
            raise ValueError("a whole-artefact reference carries no quotation digest")
        return self


# ----- record envelope ---------------------------------------------------------


class RecordEnvelope(StrictModel):
    id: RecordId
    kind: Kind
    rev: int = Field(ge=1, le=MAX_INT)
    recorded_at: Timestamp
    occurred_at: Timestamp | None
    sensitivity: Sensitivity
    lifecycle: Lifecycle
    body: JsonObject

    @model_validator(mode="after")
    def _kind_matches(self) -> RecordEnvelope:
        if kind_of(self.id) != self.kind:
            raise ValueError("id prefix must match kind")
        return self


# ----- request protocol v1 -----------------------------------------------------

Body = JsonObject


def _plain_int(value: object) -> object:
    # ``Literal[1]`` admits ``True`` and ``1.0`` (and ``Literal[0]`` admits ``False`` and
    # ``0.0``) even in strict mode; the exported JSON contracts (``const`` + ``integer``)
    # do not (review 0009 L1, decision 0014 item 5).
    if type(value) is not int:
        raise ValueError("must be the integer, not a boolean or a float")
    return value


ExactlyOne = Annotated[Literal[1], BeforeValidator(_plain_int)]
"""``1`` as a JSON integer only: for ``schema_version`` and other version constants."""
ExactlyZero = Annotated[Literal[0], BeforeValidator(_plain_int)]
"""``0`` as a JSON integer only."""


def _plain_bool(value: object) -> object:
    if type(value) is not bool:
        raise ValueError("must be a boolean, not an integer or a float")
    return value


ExactlyTrue = Annotated[Literal[True], BeforeValidator(_plain_bool)]
"""``true`` as a JSON boolean only (``Literal[True]`` also admits ``1`` and ``1.0``)."""
ExactlyFalse = Annotated[Literal[False], BeforeValidator(_plain_bool)]
"""``false`` as a JSON boolean only."""

# CP11 S11.2 (spec/03 CP11 f; decision 0062): version_of joins a preprint (source) and its
# published version (target), two works that are never merged. A06.1 (CP20.2, decision 0083)
# adds the research map: answers, addresses and draws_on, descriptive and promoting nothing.
Relation = Literal[
    "cites",
    "supports",
    "challenges",
    "supersedes",
    "depends_on",
    "used_in",
    "version_of",
    "answers",
    "addresses",
    "draws_on",
]


class CreateOp(StrictModel):
    op: Literal["create"]
    kind: Kind
    id: RecordId
    base_rev: ExactlyZero
    sensitivity: Sensitivity
    occurred_at: Timestamp | None
    body: Body

    @model_validator(mode="after")
    def _kind_matches(self) -> CreateOp:
        if kind_of(self.id) != self.kind:
            raise ValueError("id prefix must match kind")
        if self.kind == "project":
            raise ValueError("projects are created by project init, not apply")
        return self


class ReviseOp(StrictModel):
    op: Literal["revise"]
    id: RecordId
    base_rev: int = Field(ge=1, le=MAX_INT)
    sensitivity: Sensitivity
    occurred_at: Timestamp | None
    body: Body


class ArchiveOp(StrictModel):
    op: Literal["archive"]
    id: RecordId
    base_rev: int = Field(ge=1, le=MAX_INT)


class TransitionOp(StrictModel):
    op: Literal["transition"]
    id: RecordId
    base_rev: int = Field(ge=1, le=MAX_INT)
    to: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    reason: Text | None
    evidence: list[EvidenceRef] = Field(max_length=MAX_REFS)


class CorrectionApplyOp(StrictModel):
    op: Literal["correction_apply"]
    id: RecordId
    base_rev: int = Field(ge=1, le=MAX_INT)


class RelationAddOp(StrictModel):
    op: Literal["relation_add"]
    source_ref: RecordRef
    target_ref: RecordRef
    relation: Relation


class ProjectUpdateOp(StrictModel):
    op: Literal["project_update"]
    base_rev: int = Field(ge=1, le=MAX_INT)
    settings: Body


Operation = Annotated[
    CreateOp
    | ReviseOp
    | ArchiveOp
    | TransitionOp
    | CorrectionApplyOp
    | RelationAddOp
    | ProjectUpdateOp,
    Field(discriminator="op"),
]


CardText = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class DecisionCard(StrictModel):
    """A06.1 (CP20.3, decision 0084): what an owner decision is about, in the requester's words.

    The card travels with the request, so an approval request stores it in its plan. Its wording
    is generated by whoever wrote the request and is shown labelled as such. A default changes
    only derived views and generated drafts, never a record (spec/04).
    """

    what: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    why: CardText
    recommendation: CardText
    default_text: CardText
    default_at: Timestamp | None = None


class Request(StrictModel):
    schema_version: ExactlyOne
    project_id: ProjectId
    request_id: RequestId
    expected_seq: int = Field(ge=0, le=MAX_INT)
    operations: list[Operation] = Field(min_length=1, max_length=MAX_OPERATIONS)
    # A06.1, CP20.3: an optional decision card, kept in an approval request's plan.
    card: DecisionCard | None = Field(default=None, exclude_if=lambda v: v is None)


# ----- responses ---------------------------------------------------------------

ErrorCode = Literal[
    "validation_error",
    "not_found",
    "revision_conflict",
    "request_key_conflict",
    "policy_denied",
    "stale_task",
    "source_unavailable",
    "budget_exceeded",
    "busy",
    "io_error",
    "unsupported",
    "approval_required",  # A02, CP10: exit 4; details are ApprovalRequiredDetails
    "capability_unavailable",  # A02, CP10: exit 5; details are CapabilityUnavailableDetails
]

EXIT_CODES: dict[str, int] = {
    "approval_required": 4,
    "capability_unavailable": 5,
    "validation_error": 2,
    "not_found": 2,
    "unsupported": 2,
    "budget_exceeded": 2,
    "revision_conflict": 3,
    "request_key_conflict": 3,
    "stale_task": 3,
    "policy_denied": 4,
    "source_unavailable": 5,
    "io_error": 5,
    "busy": 6,
}
RETRYABLE: frozenset[str] = frozenset({"busy", "revision_conflict", "io_error"})


class OpResult(StrictModel):
    op_index: int = Field(ge=0, le=MAX_INT)
    id: RecordId | None
    rev: int | None = Field(ge=1, le=MAX_INT)


class SuccessResponse(StrictModel):
    ok: ExactlyTrue
    schema_version: ExactlyOne
    project_id: ProjectId
    request_id: RequestId
    project_seq: int = Field(ge=1, le=MAX_INT)
    results: list[OpResult]
    replayed: bool


CapabilityName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]


class OwnerRoute(StrictModel):
    """Where the owner reviews a pending approval: a hub page and an interactive command."""

    hub: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9/_.:-]{0,299}$")]
    cli: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class ApprovalRequiredDetails(StrictModel):
    """Details of ``approval_required`` (spec/04 "Pending approvals"; decision 0015).

    Nothing committed. The approval request stores the exact canonical plan; the agent
    resumes it once after the owner grants it through ``owner_route``. Never a secret."""

    approval_request_id: Annotated[
        str,
        StringConstraints(pattern=rf"^(?:apreq|mapreq)_{UUID4_PATTERN}$"),
    ]
    plan_sha256: Sha256
    required_scope: CapabilityName
    owner_route: OwnerRoute
    expires_at: Timestamp


CapabilityBlocker = Literal[
    "offline",
    "not_installed",
    "not_configured",
    "quota_exhausted",
    "unsupported_platform",
    "disabled_by_policy",
    "not_built",
]


class CapabilityUnavailableDetails(StrictModel):
    """Details of ``capability_unavailable``: which capability and what blocks it."""

    capability: CapabilityName
    blocker: CapabilityBlocker


# ----- CP10.2: owner grant and approval models (decision 0016) ---------------------------

ApprovalState = Literal["pending", "granted", "denied", "withdrawn", "expired", "revoked", "used"]
_APPROVAL_ID = rf"^apreq_{UUID4_PATTERN}$"
_CAPABILITY_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class CapabilityGrant(StrictModel):
    """What an owner channel issues: one capability for one operation, bound to an exact
    digest (a plan, request or payload) where the operation requires one, with a use count
    and a lifetime. Agents never send this; owner routes create it (the CLI's interactive
    confirmation, the paired hub session, the review queue)."""

    operation: CapabilityName
    digest: Sha256 | None
    ttl_seconds: int = Field(ge=30, le=86400)
    max_uses: int = Field(ge=1, le=100)


class CapabilityGrantReceipt(StrictModel):
    """What was issued, never the secret. A grant made from the review queue is held by the
    server: ``server_held`` is true and its approval request names it."""

    capability_id: Annotated[str, StringConstraints(pattern=_CAPABILITY_ID)] | None
    operation: CapabilityName
    digest: Sha256 | None
    expires_at: Timestamp
    max_uses: int = Field(ge=1, le=100)
    server_held: bool
    approval_request_id: Annotated[str, StringConstraints(pattern=_APPROVAL_ID)] | None

    @model_validator(mode="after")
    def _held(self) -> CapabilityGrantReceipt:
        if self.server_held != (self.approval_request_id is not None):
            raise ValueError("a server-held grant names its approval request, and only it does")
        if self.server_held != (self.capability_id is None):
            raise ValueError("a server-held grant has no capability ID to show; a client one has")
        return self


class CapabilityRevoke(StrictModel):
    """Revoke one capability, or the unused grant of one approval request."""

    capability_id: Annotated[str, StringConstraints(pattern=_CAPABILITY_ID)] | None = None
    approval_request_id: Annotated[str, StringConstraints(pattern=_APPROVAL_ID)] | None = None

    @model_validator(mode="after")
    def _one(self) -> CapabilityRevoke:
        if (self.capability_id is None) == (self.approval_request_id is None):
            raise ValueError("name exactly one of capability_id or approval_request_id")
        return self


class CapabilityRevokeReceipt(StrictModel):
    revoked: bool
    approval_request_id: Annotated[str, StringConstraints(pattern=_APPROVAL_ID)] | None = None
    state: ApprovalState | None = None


class ApprovalDecision(StrictModel):
    """The owner grants or denies exactly the plan the queue showed (its digest)."""

    plan_sha256: Sha256
    note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


ApprovalRequestIdArg = Annotated[str, StringConstraints(min_length=1, max_length=80)]


class RecommendedRequest(StrictModel):
    """A06.1, CP20.3 (decision 0084): the owner's "apply all recommended" over listed approval
    requests; ``digest`` is the previewed one (required to apply)."""

    approval_request_ids: list[ApprovalRequestIdArg] = Field(min_length=1, max_length=50)
    digest: Sha256 | None = None


class RecommendedPreview(StrictModel):
    approval_request_ids: list[ApprovalRequestIdArg]
    plan_sha256s: list[Sha256]
    operations: int = Field(ge=1)
    preview: dict[str, Any]


class RecommendedResult(StrictModel):
    result: dict[str, Any]
    closed: list[ApprovalRequestIdArg]


class ApprovalView(StrictModel):
    """An approval request as a caller who may see it is shown. Neither the grant's secret
    (never stored) nor its capability ID is part of it."""

    approval_request_id: Annotated[str, StringConstraints(pattern=_APPROVAL_ID)]
    operation: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    state: ApprovalState
    caller_rank: int = Field(ge=0, le=2)
    actor: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    request_id: Annotated[str, StringConstraints(pattern=_CAPABILITY_ID)] | None
    plan: JsonObject
    plan_sha256: Sha256
    required_scope: CapabilityName
    summary: list[Annotated[str, StringConstraints(min_length=1, max_length=300)]] = Field(
        min_length=1, max_length=50
    )
    owner_route: OwnerRoute
    created_at: Timestamp
    expires_at: Timestamp
    decided_at: Timestamp | None
    decided_by: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None
    decision_note: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None
    used_at: Timestamp | None
    used_seq: int | None = Field(ge=0, le=MAX_INT)
    closed_at: Timestamp | None
    result: JsonObject | None
    # The owner's review rendering of a pending transaction: the same server preview as
    # `decision review` (absent from agent views).
    preview: JsonObject | None = None


TYPED_ERROR_DETAILS: dict[str, type[StrictModel]] = {
    "approval_required": ApprovalRequiredDetails,
    "capability_unavailable": CapabilityUnavailableDetails,
}


class ErrorInfo(StrictModel):
    code: ErrorCode
    message: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    retryable: bool
    details: JsonObject

    @model_validator(mode="after")
    def _typed_details(self) -> ErrorInfo:
        # The A02 codes carry closed details (JSON Schema cannot express the pairing).
        typed = TYPED_ERROR_DETAILS.get(self.code)
        if typed is not None:
            typed.model_validate(self.details, strict=True)
        return self


class ErrorResponse(StrictModel):
    ok: ExactlyFalse
    error: ErrorInfo
    project_seq: int | None = Field(ge=0, le=MAX_INT)


class PrometheosError(Exception):
    """Application error carrying a stable public code."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        if code not in EXIT_CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = details or {}
        self._degrade_malformed_details()

    def _degrade_malformed_details(self) -> None:
        """A02 codes carry closed details. Malformed ones are a server defect: report a
        plain ``validation_error`` naming the intended code (never a raw pydantic error, and
        never the malformed details, which could hold anything; decision 0015 round 2)."""
        typed = TYPED_ERROR_DETAILS.get(self.code)
        if typed is None:
            return
        try:
            typed.model_validate(self.details, strict=True)
        except ValidationError:
            intended = self.code
            self.code = "validation_error"
            self.message = f"internal error: malformed {intended} details ({self.message})"[:4096]
            self.details = {"intended_code": intended}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]

    def to_response(self, project_seq: int | None = None) -> ErrorResponse:
        self._degrade_malformed_details()  # details are mutable after construction
        return ErrorResponse(
            ok=False,
            error=ErrorInfo(
                code=self.code,  # type: ignore[arg-type]
                message=self.message,
                retryable=self.code in RETRYABLE,
                details=self.details,
            ),
            project_seq=project_seq,
        )


def depth_error(exc: JSONDepthError) -> PrometheosError:
    """The typed error for a JSON depth refusal (decision 0029). Outside input that is too
    deep is the caller's ``validation_error``. The application's own ceiling is a
    deterministic limit of this build, neither the caller's invalid JSON nor retryable, so it
    is ``budget_exceeded`` (which ``RETRYABLE`` excludes) and says so."""
    if exc.internal:
        return PrometheosError(
            "budget_exceeded",
            f"a stored or returned value is nested more than {exc.limit} levels deep, "
            "deeper than this build can handle",
            {"limit": exc.limit},
        )
    return PrometheosError("validation_error", f"not valid JSON: {exc}"[:300], {})


# ----- A02, CP10.6: owner-route contracts (decision 0022 item 5) --------------------------------

CredentialReference = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")]


class ExactPlanRequest(StrictModel):
    """The owner applies exactly one previewed plan (an Overleaf push): the digest the preview
    showed. A single-use capability bound to it is issued and spent in one step; a changed
    plan is refused. ``request_id`` is only for an agent's approval request of the same plan."""

    digest: Sha256
    request_id: RequestId | None = None


class OverleafPushResult(StrictModel):
    """What a push of an authorised plan reports: the commits pushed, the remote head, the
    commit that spent the authorisation and the limitations of the claim."""

    pushed: list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]] = Field(
        max_length=1000
    )
    remote_head: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")] | None
    authorised: SuccessResponse
    limitations: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = Field(
        max_length=50
    )


class ProviderOwnerRunRequest(StrictModel):
    purpose: str
    meeting_id: str | None = None
    context_budget: int = Field(default=12000, ge=1000, le=200_000)
    include_restricted: bool = False
    digest: Sha256
    confirm_restricted: bool = False
    acknowledge_unknown: str | None = None
    request_id: str | None = None
    wait_seconds: int = Field(default=0, ge=0, le=20)


class ProviderOwnerConfigureRequest(StrictModel):
    config: JsonObjectInput
    digest: Sha256 | None = None
    request_id: str | None = None
    revision: int | None = Field(default=None, ge=0, le=MAX_INT)


class ProviderOwnerConfigureResult(StrictModel):
    revision: int
    route: str
    policy: str


class ProviderApproval(StrictModel):
    """The owner approves one previewed provider request or configuration: the operation and
    the digest `provider preview` or `provider configure --preview` showed. The CLI prints a
    single-use capability for it once (stderr); the hub spends one in the same step."""

    operation: Literal["provider_run", "provider_egress", "provider_configure"]
    digest: Sha256


class ProviderSecretSet(StrictModel):
    """The owner stores a credential in the OS credential store. The value is entered in the
    owner's own interface, bound to nothing but the reference, and never returned or logged."""

    reference: CredentialReference
    from_env: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None


class RootChangePreview(StrictModel):
    """Only the trusted owner's exact root-change preview may expose these path fields."""

    alias: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]
    old_path: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None
    new_path: Annotated[str, StringConstraints(min_length=1, max_length=1024)] | None


class ProviderSecretDelete(StrictModel):
    reference: CredentialReference


class CredentialHealth(StrictModel):
    """A credential as anyone may see it: its reference and whether a value exists. Never the
    value, its length or any fragment."""

    reference: CredentialReference | None
    present: bool
    store: Annotated[str, StringConstraints(min_length=1, max_length=100)]


class ProviderSecretSetResult(StrictModel):
    credential: CredentialHealth


class ProviderSecretDeleteResult(StrictModel):
    removed: bool
    credential: CredentialHealth


# ----- A07, CP21.2: remembered devices (decision 0089) ------------------------------------

_DEVICE_ID = r"^dev_[0-9a-f]{16}$"


class DeviceListRequest(StrictModel):
    """No parameters: the home's remembered devices."""


class DeviceView(StrictModel):
    device_id: str = Field(pattern=_DEVICE_ID)
    label: str = Field(max_length=80)
    state: Literal["active", "expired", "revoked"]
    created_at: str
    last_used_at: str
    expires_at: str
    revoked_at: str | None


class DeviceListResult(StrictModel):
    devices: list[DeviceView]


class DeviceRevokeRequest(StrictModel):
    device_id: str = Field(pattern=_DEVICE_ID)


class DeviceRevokeResult(StrictModel):
    device_id: str = Field(pattern=_DEVICE_ID)
    revoked: bool
