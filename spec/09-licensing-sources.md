# Licensing, reuse and research basis

## Chosen direction and limits

**Owner decision, 2026-10-18 (spec 1.5.2, decision 0072).** The owner chose **Apache-2.0 for the whole implementation**: core, hub, desktop shell, agent package, schemas, skills and tools, with a root `LICENSE` and `NOTICE` and a third-party notices file per shipped artifact. The recommendation below is kept as the reasoning the owner weighed; it no longer directs licensing. Publication itself stays behind E08, recorded by the owner after the release inventory (`docs/release/inventory.md`).

The package's original recommendation follows. Recommend **AGPL-3.0-or-later for newly authored core/hub**, with **Apache-2.0 for independently separable schemas, original skills and small client helpers**. Keep boundaries and per-directory licenses explicit. This favors an open, agent-friendly ecosystem while requiring source availability in circumstances covered by AGPL, including its modified network-interaction provision. It does not prevent competitors from copying, hosting or improving code in compliance with the license. Read the actual obligations, not a marketing summary. [AGPL text via OSI](https://opensource.org/license/agpl-3.0)

An OSI-open-source license cannot prohibit copying, modification or commercial competition. Copyright protects expression rather than ownership of the underlying idea; the cited copyright explanation is US-specific, not a determination of the founder's jurisdiction. Brand/trademark policy can distinguish official releases but is not a monopoly on functionality. If preventing competitive reuse is nonnegotiable, the owner must choose source-available terms and stop describing them as OSI open source. This package recommends open source and does not quietly switch later to a restrictive license. [Open Source Definition](https://opensource.org/osd), [US Copyright Office explanation](https://www.copyright.gov/help/faq/faq-protect.html)

The existing repository's LICENSE was inspected: MIT, copyright 2026 PrometheOS contributors. This package does not retroactively relicense it, erase its notices or claim sole rights over prior contributions. New development can begin privately under the recommended design; public licensing requires a truthful ownership/reuse inventory and the owner's decision. Do not assume affiliation, nationality, employment IP terms or third-party assignment. Obtain targeted legal advice when those facts or a concrete compatibility issue warrant it; a blanket unexplained “legal approval” gate must not halt unrelated original private development.

Documentation licensing and brand policy are owner decisions recorded before public distribution; do not apply a fresh license to third-party input designs simply because they were supplied. Research data/manuscripts/recordings remain separately owned and licensed. A software license does not grant permission to redistribute papers or participant recordings.

## Reuse register and release obligations

At adoption record: component and exact version/commit; retrieval date and canonical source; file/component scope; SPDX expression or unknown; license file digest; copied/linked/adapted/dependency mode; local modifications; required copyright/license/NOTICE texts; compatibility rationale; unresolved issues; reviewer and evidence. Retain upstream notices exactly when required. MIT generally requires its copyright and permission notice; it does not universally require a separate NOTICE file. Apache components have their own notice/modification obligations. Dependencies of dependencies and redistributed model/decoder assets need review too.

**Rights inventory at every checkpoint (A02).** From CP10 on, each checkpoint enters every new dependency, executable, model weight file, tokenizer, language pack, dataset, template, citation style and other bundled or installable asset in `development/dependencies.json` before use. Each entry records the exact version or revision hash, terms, required notices and redistribution conditions. A component with unknown rights is neither distributed nor offered for install. The register gains the entry kinds it needs when the first such asset arrives. CP6 and CP9 keep the 1.1.0 register rules above, which already cover their dependencies and redistributed model and decoder assets, and C08-RIGHTS checks their entries with the rest of the v1 inventory. C12-MODELS checks model offers, and the self-contained profile's release checkpoint (a later amendment) checks the rest. A software licence does not license research papers, abstracts or participant recordings.

No license in a README is not permission. A skills repository may have different per-file or bundled-content rights: inspect the actual distribution. Do not assume the umbrella license cures unknown provenance. Forking preserves obligations. “Recreating” copied expression does not remove them. Prefer clean integration through documented interfaces; independently authored replacements must record their source basis.

Use DCO 1.1 for contributions, with actual contributor sign-off, not fabricated by an agent. DCO records a contributor's certification; it is not copyright assignment and does not automatically grant unilateral future relicensing rights. A future CLA/dual-license plan requires a separate explicit decision. [Developer Certificate of Origin](https://developercertificate.org/)

Release gate: exact dependency lock and reuse register, required notices, ownership questions affecting release resolved or excluded, source distribution instructions and licenses matching shipped artifacts, owner publication authorization. Do not publish as part of a passing test.

## Initial adoption queue

These are selected technologies or research candidates, **not approved exact dependency distributions**. Version, transitive dependencies and rights must be checked at their checkpoint. Store the resulting register in `development/dependencies.json` at CP0 and update when introducing a dependency.

| Component | Decision | Verification point |
|---|---|---|
| Python, SQLite, Typer, Pydantic, uv/hatchling, test tools | Core tooling | CP0/1 |
| Official MCP Python SDK | Transport adapter | CP3 |
| Agent Skills format | Compatible skill structure, original initial skills | CP3 |
| FastAPI/Uvicorn, React/Vite, PDF viewer | Local hub; viewer only if required | CP5 |
| Audio capture binding, decoder, faster-whisper/model | Optional adapters after live feasibility and per-asset review | CP6 |
| Six ecosystem repositories below | Integrate/select/adapt only for demonstrated gap; no wholesale vendor | CP4 or later feature needing one |
| Overleaf Git/local handoff | Local handoff plan (CP7); Git synchronisation connector under A01 | CP7/CP9 |
| Tectonic TeX engine (A01) | Pinned release, explicit install, license and bundle terms checked | CP9 |
| CodeMirror 6, LaTeX language mode, PDF.js (A01) | Hub editor and PDF preview; exact versions and licenses recorded | CP9 |
| Operation registry, run contract, maintenance service (A02) | Original code; no new dependency expected | CP10 |
| BibTeX/RIS/CSV parsing, identifier normalisation, PDF text extraction (A02) | One library per need after licence review; an extraction library's model assets are separate entries (for example Docling's code is MIT while its models have their own terms) | CP11 |
| Zotero desktop (A02) | Integrated through its local API, never bundled or redistributed. AGPL-3.0 desktop application installed by the owner (decision 0010); the owner enables the local API in Zotero's settings | CP11/CP16 |
| Local AI candidates (A02): SQLite FTS5, scikit-learn, Sentence Transformers (optionally ONNX Runtime), a small embedding model such as all-MiniLM-L6-v2, an optional reranker, Tesseract and its language data, faster-whisper or whisper.cpp, llama.cpp with compact Qwen3-1.7B weights | One choice per capability. Each runtime, model, quantisation, tokenizer and language pack gets its own entry. The licences stated in the roadmap proposal are a 2026-10-05 snapshot to recheck | CP12 |
| Public scholarly APIs (A02): Crossref, arXiv, OpenAlex, Europe PMC, PubMed/NCBI, Unpaywall; optionally DOAJ and Semantic Scholar | Terms, quotas and contact requirements kept as adapter metadata; retrieved records keep their own rights (abstracts may be licensed separately); Semantic Scholar needs a use-licence review first | CP13 |
| Citation formatting (A02): Citation.js or citeproc-py, CSL styles | Style files carry their own licences | CP13/CP15 |
| Google Calendar API (A02, optional) | OAuth client terms and quotas reviewed; never required by the core | CP16 |
| Work items, event outbox, detectors and queue (A05) | Original code; no new dependency expected | CP18 |
| Desktop shell framework (A05): Electron 44.5.1 (MIT, with Chromium's bundled notices) + electron-builder 26.17.0 (MIT; NSIS per-user installer), the owner's choice (decision 0049) after the CP19.1 spike compared Tauri 2, pywebview and Electron (decisions 0047 and 0050) | Versions, installer tooling terms and the code-signing requirement are recorded in decision 0050 and Electron's notices in decision 0047; the transitive notices and the `development/dependencies.json` entries are recorded with the shipped shell package (CP19.2) | CP19 |
| Agent package (A05): Claude Code plugin format, configuration for one non-Claude MCP host | Original package content; host formats integrated through their documented configuration, never by copying host code | CP19 |
| tree-sitter and tree-sitter-matlab (A05) | MIT; exact grammar revision recorded; the MATLAB/Simulink module is original code that may later be offered upstream only with owner approval | CP17 |
| Graphify (A05, optional input) | Never bundled or forked; an existing `graph.json` is read with staleness checks; licence and version of the user's install recorded when read | CP17 |
| Ollama (A05, optional external) | Installed by the user and reached on loopback (D13 pattern C); never bundled | CP12 |
| OpenRouter (A07, optional service) | The first remote route of the intelligence layer; paid per use under the owner's own account and key; terms, data policy, zero-data-retention routing and model metadata recorded and rechecked at CP22; never required | CP22 |
| Local embedding and reranking (A07): a small embedding model such as bge-small-en-v1.5 through a CPU runtime such as fastembed (ONNX Runtime), optionally a small cross-encoder reranker | Exact model revisions, the weights' licences and runtime notices recorded before adoption; installed by explicit owner action (spec/08); candidates, not commitments | CP22 |
| uv as the per-user installer (A07) | The core installs as a uv tool; uv itself is installed by the user and never bundled; licence recorded | CP21 |
| Node-RED and ntfy (A05, optional externals) | Self-hosted by the user; reached only through signed webhooks after a later amendment defines rules; never bundled | after A06 |
| Zotero 10 (A05) | Zotero remains external and never bundled; the CP11 adapter targets the installed version recorded at E10 | CP11/CP16 |

Under A05 (D13) a service is integrated through its official API or MCP server before anything is built, and Claude Science is not a target host. Proprietary skills or document generators may be named in documentation, never copied.

## Evidence and source register

Research checked 2026-10-04; rows marked A02 come from the roadmap proposal's 2026-10-05 snapshot and are rechecked at the adopting checkpoint. Upstream pages and provided documents inform design choices; none establish that PrometheOS or an integration has passed runtime tests. Input-design provenance hashes are in `../design-inputs.json`. No inherited clone SHA or benchmark is promoted to independently verified evidence.

| Source | Design use and limit |
|---|---|
| [SQLite transactions](https://sqlite.org/lang_transaction.html), [atomic commit](https://www.sqlite.org/atomiccommit.html), [backup API](https://sqlite.org/backup.html) | Existing transaction/recovery mechanisms reduce bespoke persistence work. Hardware durability still depends on OS/storage behavior. |
| [SQLite WAL](https://sqlite.org/wal.html) | Informs rejection of network/shared live authority; v1 deliberately selects rollback journal rather than adding WAL operational complexity. |
| [Agent Skills specification](https://agentskills.io/specification) | Standard SKILL.md directory format and progressive references; PrometheOS sidecar remains explicitly PrometheOS-specific, not an invented universal standard. |
| [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | Reuse protocol implementation and generated schemas; exact tested release selected at CP3. |
| [OpenScience](https://github.com/synthetic-sciences/openscience) | Research workflow ecosystem and interoperability candidate. Complement execution/research capabilities with durable project evidence. |
| [K-Dense scientific-agent-skills](https://github.com/K-Dense-AI/scientific-agent-skills) | Scientific procedure library candidate; selective import rather than whole-library prompt loading. |
| [Orchestra AI-Research-SKILLs](https://github.com/Orchestra-Research/AI-Research-SKILLs) | Research skill packaging and procedure candidate; evaluate relevance to engineering workflow and per-file rights. |
| [AI-Scientist-v2](https://github.com/SakanaAI/AI-Scientist-v2) | Autonomous research system reference; do not adopt its orchestration objective as PrometheOS scope. |
| [EvoScientist](https://github.com/EvoScientist/EvoScientist) | Agent research system reference and potential artifact producer; no requirement to reproduce its runtime. |
| [K-Dense BYOK](https://github.com/K-Dense-AI/k-dense-byok) | Provider-backed research UX reference; optional bounded generation is a narrower responsibility here. |
| [Overleaf Git documentation](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration) | Git handoff has account and history/track-change limitations. Do not promise full comments or tracked-change synchronization. Recheck before implementing. |
| [SQLite FTS5](https://www.sqlite.org/fts5.html) (A02) | Lexical BM25 baseline for local search; no vector service needed. |
| [Zotero local API](https://www.zotero.org/support/dev/web_api/v3/local_api) (A02) | Local library access without a cloud account. Probe the installed version; recheck what writing it supports before CP16 offers any write-back. |
| [Crossref access](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/), [arXiv API terms](https://info.arxiv.org/help/api/tou.html), [OpenAlex authentication](https://help.openalex.org/api/authentication/), [Europe PMC](https://europepmc.org/RestfulWebService), [PubMed/NCBI](https://support.nlm.nih.gov/knowledgebase/article/KA-05317/en-us), [Unpaywall](https://unpaywall.org/products/api) (A02) | Free discovery access positions as of 2026-10-05. Quotas, metering and terms change, so adapters hold them as metadata. No source gives exhaustive coverage; Unpaywall's retired search endpoint is not used. |
| [scikit-learn](https://scikit-learn.org/stable/about.html), [Sentence Transformers efficiency](https://www.sbert.net/docs/sentence_transformer/usage/efficiency.html), [ONNX quantization](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html), [ASReview](https://github.com/ASReview/asreview) (A02) | Building blocks for CPU-first retrieval, classification and active screening. Not PrometheOS performance evidence. |
| [OSF Projects API transition](https://help.osf.io/article/744-osf-projects-api-impact) (A02) | OSF Projects become read-only in February 2027; build no storage on OSF Projects writes. |
| [Google Calendar sync](https://developers.google.com/workspace/calendar/api/guides/sync), [Calendar quota](https://developers.google.com/workspace/calendar/api/guides/quota) (A02) | Incremental sync tokens and quota rules for the optional calendar connector. |

The opportunity is an **inference**, not a claim that no competitor exists: connect evolving scientific evidence, meetings, scoped corrections, next steps and papers across replaceable coding agents, with a useful CLI and a quiet researcher UI. Its value must be demonstrated in the live researcher gate. Repository stars or an attractive agent demo cannot establish that gap or validate willingness to use the product.

Only broaden research at a checkpoint when it resolves a concrete decision: select an audio binding, verify a license, test a host transport or identify a maintained library. Do not restart an unbounded landscape survey before implementing the settled backbone.
