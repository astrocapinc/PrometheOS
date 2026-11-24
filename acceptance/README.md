# Acceptance inventory

`scenarios.json` contains synthetic journey intent. `../checkpoints.json` contains all required technical check IDs, including infrastructure checks not repeated as user scenarios. These are test specifications to implement at the named checkpoint. Merely parsing them does not pass them.

Development receipts use `../schemas/receipt.schema.json`; begin from `../templates/receipt-template.json`. The template deliberately contains unbound hashes and no passes. JSON Schema validates shape; CP0 must implement semantic validation and promotion rules from the delivery spec.

Candidate tree identity: create a sorted manifest of repository-relative implementation/spec/config/test files and their SHA256 values. Exclude `.git`, caches, virtual environments, generated build outputs, local secrets/stores and development receipt/output files to avoid recursive hashing. Include lockfiles and source assets. Hash the UTF-8 canonical JSON manifest and retain it as evidence. Record git revision where available plus dirty state; a revision alone cannot certify uncommitted changes.

Evidence paths are relative to the package root, cannot escape it or traverse symlinks, and reference saved outputs. A passing command check needs exit code 0 and actual output evidence; a manual/live/field check needs observer, steps, environment and a result report. All required check IDs must appear exactly once in the checkpoint receipt. External IDs are separate. Schema validity never establishes that a command ran: independent review checks the actual files/observations.

Full product acceptance requires E01–E07 (E07, the Overleaf Git roundtrip, became required under amendment A01). Under amendment A02 full v1 product acceptance also requires E09 and E10, and the later self-contained research profile requires E11–E13 (E14 is optional). Public publication additionally requires E08. Future code must not use a single aggregate “green” flag to collapse those distinctions.

From CP10 on, each checkpoint has a walk check (C10-WALK to C19-WALK, scenarios S28, S33, S38, S42, S44, S46, S48, S52, S55 and S58; A05 added the last three). A walk runs the scenario on the actual built app against a fresh store from synthetic fixtures with browser steps that assert stored state, repeats it on a second fresh store through MCP, compares the normalised outcomes and saves a transcript that the independent reviewer reads. The KERS-inspired scenarios K1–K8 that the walks use are listed in `../spec/10-delivery.md`.
