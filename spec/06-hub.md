# Researcher hub

The hub is a required v1 interface at CP5. Its purpose is fast orientation and review while the coding agent remains the execution surface. It shares the CLI store and operations. It is not a second truth, independent agent chat or general IDE.

## Information architecture

Four primary destinations: **Now**, **Notebook**, **Knowledge**, **Papers**. A persistent compact project header shows project, source-coverage warning when relevant and agent connection guidance. Meetings, actions, people, recordings and workflows are contextual pages/panels reached from these destinations; settings does not occupy the main research path. Do not add a dashboard tile for every backend entity.

| Destination | Initial screen | Expand on demand |
|---|---|---|
| Now | Current objective with the deliverables and their stages (A06.1), decisions waiting for the owner as cards, verification checks planned, then changes since last visit, what is due, what is running, results awaiting interpretation, approvals waiting for the owner (A02), next adopted actions, one prominent capture action | Why this action, blockers, people and meeting history, run progress, flagged dependents |
| Notebook | Readable dated entries with meaningful headings, source indicators and correction badges | Exact source, previous revision, generated-view provenance, assumptions a finding relies on (A02) |
| Knowledge | Search and filters for findings, evidence, questions and scoped claims; the literature library and evidence matrices (A02) | Backlinks, comparison of scope, small relationship view, matrix cell sources, inquiries, screening and coverage (A02) |
| Papers | Deliverable stage, evidence gaps, next review tasks, manuscript preview; under A06.1 each paper's questions with their claims, one-line derived status and its basis, and what moves it to the next stage | Venue requirements, author work, anchors, review rounds, the LaTeX manuscript workspace (A01) and deliverable recipes (A02) |

A02 adds no primary destination. Literature lives under Knowledge, deliverable recipes under Papers and meeting pages, and running work, results awaiting interpretation and pending approvals on Now. Model, provider and connector settings sit in the settings area outside the main research path. When a project cannot open, a recovery screen appears before any destination loads. Every hub action calls a registry operation that MCP and HTTP also expose; the frontend holds no privileged business logic.

Capture accepts typed note, meeting notes, selected files or recording. It begins with a small form; optional fields appear when the user chooses a type. One ingestion job has one progress/result surface. Avoid repeated modal confirmations for ordinary notes permitted by policy.

## Progressive disclosure and source fidelity

Each card leads with one useful statement and its state in plain language. Technical IDs, hashes, raw event JSON, provider tokens and internal check names are hidden behind Details. Citations use recognizable titles/locators. Generated wording is labeled and can be compared to original text in a context panel. Relevant source spans are highlighted without altering the original. Show a source as unavailable when missing, never a fabricated excerpt.

The context panel contains the selected original text/figure/PDF and related finding/action. It opens without taking the user away from their current list. Keyboard focus, browser back, selection and scroll position remain sensible. External file opens are explicit. PDFs and images use safe viewer components; no executing document macros or source HTML.

“Proposed correction” and “Applied correction” must look and behave differently. Before application show a preview of affected entries/claims/paper anchors; after success show the committed revision and unresolved follow-up work. Do not animate propagation before the server commits. Conflicts preserve the user's draft and offer refresh/review, not silent overwrite.

When a paired owner reviews a project-root setting change, show the root alias and exact old → new local path values before the decision. This is A03's narrow trusted-review exception: only the structured `root_changes[]` fields of that root-setting approval or decision preview may retain those paths (spec/04). Added and removed paths show an explicit absent value. Agent replies, ordinary owner reads, errors, run views and arbitrary effect text keep their existing path redaction. The guard uses the paired-owner principal, the two permitted preview routes and the structured fields together; a label is refused as path input and never silently replaces a stored path.

## Required interaction paths

1. **Return to project:** identify current objective, latest meaningful change and next adopted action; follow why/evidence without searching raw logs.
2. **Review a meeting:** import typed notes, inspect extracted suggestions beside evidence, adopt one action, leave ambiguous ownership unresolved; see the new action on Now.
3. **Resolve a correction:** compare old finding/new evidence, approve a correction, see affected manuscript use flagged and next context reflect it.
4. **Audit a result:** navigate notebook → quantity → exact retained source/version → execution receipt and scope limitations.
5. **Prepare a paper:** locate a missing venue requirement/evidence gap, open the linked source, assign or draft the next step, preview a local manuscript.
6. **Use a skill:** inspect provenance/license, preview project-local installation and read the recipe/expected artifact; no code executes during preview.
7. **Record a meeting:** choose devices, see active capture, stop, inspect retained file/transcript, extract proposals; failures retain recoverable segments.
8. **Write the manuscript (A01):** open the paper's LaTeX workspace, edit a section, see a linked claim beside the line, compile, jump from a diagnostic and from the PDF to source, pull an Overleaf change and push an owner-approved commit.
9. **Resume after a closed chat (A02):** read on Now what changed, what is running and which results await interpretation; open a finished run, record its interpretation and see it leave the queue.
10. **Approve an agent request (A02):** open a pending approval from Now, read the exact plan and its effects, grant or deny it, and see the agent's work continue or stop.
11. **Recover a project that will not open (A02):** follow the recovery screen to inspect health, verify a backup and restore into a new store, or grant an agent's pending request to switch to a restored store, with the damaged store kept.
12. **Build an evidence matrix (A02):** import a bibliography and PDFs, search, classify, fill cells from page-located quotations, see abstract-only items marked, and see a correction flag a dependent row.
13. **Run a literature review (A02, CP13–CP14):** save a question and criteria, preview and run discovery, screen with reasons, read the coverage, save a cited synthesis and set a watch.
14. **Manage local AI (A02, CP12):** preview, install, benchmark, unload and remove a model, and choose the routing policy for a workflow.
15. **Prepare a supervisor meeting (A02, CP15):** render an agenda and brief from reviewed evidence, see a stale flag after a correction and regenerate.
16. **Connect a library or calendar (A02, CP16):** connect, sync, resolve a conflict and disconnect while keeping local records.
17. **Follow the research map (A06.1, CP20):** see each deliverable's stage, open the step to read what moves it forward, open a claim under its question to read its status, basis, causes and verifiables (artefact, producer, digest, receipt, expected, measured and tolerance), answer one decision card and see its default and outcome, and confirm an adoption outline once.
18. **Open the application (A07, CP21):** run `prometheos start`, install the hub as an app from the browser, close it and start it again after a core restart without pairing again; revoke a remembered device and see it asked to pair.
19. **Use the intelligence layer (A07, CP22):** enter the key through the owner's credential entry (never a hub form), set each adoption root's sensitivity, grant a standing policy with its task kinds, sensitivity ceiling, models and caps, see the month's spend and each call's model and cost, see every model-assisted item tagged with its source and fallback, accept or reject a proposal, and revoke the policy; with no key every page still works and says what it did instead.

## Accessibility and quality

Target WCAG 2.2 AA practices: semantic landmarks, keyboard paths, visible focus, descriptive labels, adequate contrast, reduced-motion preference, text alongside color and accessible status announcements. CP5 automated accessibility checks cover core routes; manual keyboard testing is separate. Do not claim full conformance from an automated score.

Responsive minimum viewport 1024×768 for full desktop workflows; 390px width supports reading/capture with stacked panels. No requirement for a native mobile app. Empty/loading/error/offline/stale/permission-denied states are specified for each route and tested. The hub must open without a provider key, and model failure must not leave a blank screen.

Visual style: restrained typography, generous but functional spacing, low-chrome navigation, one clear primary action per screen, consistent status language. Premium means clarity and reliable interactions, not gradients or dense charts. CP5 includes actual browser QA of the built wheel assets on Windows, plus assertions on the resulting store and API behavior. Screenshots supplement interaction evidence.

## Reducing window switching

Keep orientation, source excerpts, notebook, meeting reconciliation, result previews and paper readiness in the hub. Keep execution in the coding agent. Use deep links and copyable context to connect them. Offer explicit “Open in existing tool” actions where editing requires MATLAB or a PDF editor. LaTeX manuscripts are edited in the hub workspace (A01); Overleaf remains reachable for collaborators, comments and tracked changes. Measure avoided searches and repeated explanations in real sessions; do not declare that every program has been replaced.

## Desktop application (A05, CP19)

- **A window on the hub.** The hub is also delivered as a Windows desktop application whose window shows the same loopback hub, so every hub path above applies unchanged.
- **Tray.** The application starts and stops the local core, shows its state in the tray, and offers open, capture and quit.
- **Notifications.** System notifications carry events (for example an approval request or an approaching deadline). Opening one leads to the relevant hub view. Notifications never grant approvals.
- **Capture.** A global hotkey opens quick capture.
- **Single instance.** A second start focuses the running instance.
- **No new authority.** The window pairs like any hub client and has no authority beyond the owner session it holds.
- **Work items.** The hub (and so the window) has views to add, edit and complete work items with deadlines, and an inbox of unacknowledged events in Now.
