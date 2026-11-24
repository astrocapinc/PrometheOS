"""Registry of exported public JSON Schemas.

``tools/check_contracts.py`` compares these generated schemas with the committed
copies under ``contracts/schemas`` and validates positive/negative examples.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from prometheos.adoption import contracts as adoption
from prometheos.domain import bodies, events, models
from prometheos.library import contracts as library_contracts
from prometheos.store import operational

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

REGISTRY: dict[str, type[BaseModel]] = {
    "record_ref": models.RecordRef,
    "evidence_ref": models.EvidenceRef,
    "record_envelope": models.RecordEnvelope,
    "request": models.Request,
    "success_response": models.SuccessResponse,
    "error_response": models.ErrorResponse,
    "project_settings": bodies.ProjectSettings,
    "source_body": bodies.SourceBody,
    "version_body": bodies.VersionBody,
    "execution_receipt_v1": bodies.ExecutionReceiptV1,
    "entry_body": bodies.EntryBody,
    "claim_body": bodies.ClaimBody,
    "assessment_body": bodies.AssessmentBody,
    "correction_body": bodies.CorrectionBody,
    "person_body": bodies.PersonBody,
    "role_body": bodies.RoleBody,
    "meeting_body": bodies.MeetingBody,
    "item_body": bodies.ItemBody,
    "action_body": bodies.ActionBody,
    "paper_body": bodies.PaperBody,
    "anchor_body": bodies.AnchorBody,
    "job_body": bodies.JobBody,
    # A02, CP10 (decision 0015)
    "execution_body": bodies.ExecutionBody,
    "approval_required_details": models.ApprovalRequiredDetails,
    "capability_unavailable_details": models.CapabilityUnavailableDetails,
    # CP10 operational table rows (project store schema 7, registry schema 2)
    "operation_run": operational.OperationRun,
    "operation_run_log": operational.OperationRunLog,
    "approval_request": operational.ApprovalRequest,
    "backup_inventory_entry": operational.BackupInventoryEntry,
    "maintenance_approval": operational.MaintenanceApproval,
    "store_quarantine": operational.StoreQuarantine,
    # A02, CP10.2 (decision 0016): owner grants and approval views
    "capability_grant": models.CapabilityGrant,
    "capability_grant_receipt": models.CapabilityGrantReceipt,
    "capability_revoke": models.CapabilityRevoke,
    "capability_revoke_receipt": models.CapabilityRevokeReceipt,
    "approval_decision": models.ApprovalDecision,
    "approval_view": models.ApprovalView,
    # A06.1, CP20.3 (decision 0084): "apply all recommended"
    "recommended_request": models.RecommendedRequest,
    "recommended_preview": models.RecommendedPreview,
    "recommended_result": models.RecommendedResult,
    # A06.1, CP20.4 (gap audit #16): the hub's verification budget control
    "verification_budget_change": bodies.VerificationBudgetChange,
    # A02, CP10.3 (decision 0017): run stages (store schema 8) and the run view
    "operation_run_stage": operational.OperationRunStage,
    "operation_run_view": operational.RunView,
    # A02, CP10.5 (decision 0021): a maintenance approval as its reader is shown
    "maintenance_approval_view": operational.MaintenanceApprovalView,
    # A02, CP10.6 (decision 0022): owner-route request and result contracts
    "exact_plan_request": models.ExactPlanRequest,
    "overleaf_push_result": models.OverleafPushResult,
    "provider_approval": models.ProviderApproval,
    "provider_owner_run_request": models.ProviderOwnerRunRequest,
    "provider_owner_configure_request": models.ProviderOwnerConfigureRequest,
    "provider_owner_configure_result": models.ProviderOwnerConfigureResult,
    "provider_secret_set": models.ProviderSecretSet,
    "provider_secret_set_result": models.ProviderSecretSetResult,
    "provider_secret_delete": models.ProviderSecretDelete,
    "provider_secret_delete_result": models.ProviderSecretDeleteResult,
    # A07, CP21.2 (decision 0089): remembered devices
    "device_list_request": models.DeviceListRequest,
    "device_list_result": models.DeviceListResult,
    "device_revoke_request": models.DeviceRevokeRequest,
    "device_revoke_result": models.DeviceRevokeResult,
    # A05, CP18.2 (decision 0035): the research event catalogue (store schema 9 outbox row)
    "event": events.Event,
    # A05, CP17.1 (decision 0039): adoption scans, inventory rows, analyzers, evidence, profiles
    "adoption_scan": adoption.AdoptionScan,
    "adoption_file": adoption.AdoptionFile,
    "adoption_analyzer": adoption.AdoptionAnalyzer,
    "adoption_evidence": adoption.AdoptionEvidence,
    "adoption_profile": adoption.AdoptionProfile,
    # A05, CP17.3 (decision 0041): families and ranked members
    "adoption_family": adoption.AdoptionFamily,
    # A05, CP17.6 (decision 0045): the baseline package and its import
    "adoption_baseline_package": adoption.AdoptionBaselinePackage,
    "adoption_baseline_import": adoption.AdoptionBaselineImport,
    # A02, CP11 S11.1 (decision 0060): the work kind and the extracted page-text payload
    "work_body": bodies.WorkBody,
    "pdf_pages_v1": library_contracts.PdfPagesV1,
    # A02, CP11 S11.4 (decision 0064): the evidence matrix and its rows
    "matrix_body": bodies.MatrixBody,
    "extraction_body": bodies.ExtractionBody,
}


def schema_for(name: str) -> dict[str, Any]:
    model = REGISTRY[name]
    schema = model.model_json_schema(mode="validation")
    schema["$schema"] = SCHEMA_DIALECT
    schema["$id"] = f"https://prometheos.local/contracts/{name}.schema.json"
    return schema


def schema_text(name: str) -> str:
    return json.dumps(schema_for(name), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
