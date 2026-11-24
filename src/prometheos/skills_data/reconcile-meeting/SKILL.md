---
name: reconcile-meeting
description: Turn captured meeting notes into proposed observations, suggestions, requests, decisions, commitments and questions in PrometheOS. Use after a supervision or project meeting has been captured, or when the user asks to extract actions or reconcile a meeting with current work.
compatibility: Needs the PrometheOS MCP server (prometheos mcp serve --stdio) or the prometheos CLI.
license: Apache-2.0
metadata:
  author: prometheos
  version: "1.0"
---

# Meeting intake as proposals

The researcher captures notes (`prometheos meeting capture`). You classify; the researcher
adopts. Nothing you submit becomes a commitment by itself.

## Steps

1. `task_prepare {"purpose": "meeting_extract", "meeting_id": "meeting_..."}`. The context
   contains the notes with line numbers and the source/version IDs to cite.
2. For each meaningful statement, propose one item:
   - `type`: observation, suggestion, request, decision, commitment or question.
   - `text`: the statement, close to the original wording.
   - `evidence`: the exact line locator in the notes version.
   - `proposed_owner` / `due_at`: **null unless the notes state them explicitly**.
3. Classify carefully. "Perhaps compare..." from a supervisor is a *suggestion*, not a
   commitment. "I will attach..." is a commitment by the speaker. A question with no agreed
   owner keeps no owner.
4. `task_submit {"task_id": "...", "result": {"purpose": "meeting_extract", "items": [...]},
   "request_id": "<new UUID4>"}`.
5. Show the researcher `prometheos meeting reconcile --meeting ...`: proposed items beside
   current actions. Similar topics are only candidates; never merge them yourself.

## Rules

- The same meeting and exact line locator cannot be submitted twice; a conflict means the item
  already exists.
- Adoption (creating a ready action from a commitment) is the researcher's decision through
  `prometheos decision review`. Do not phrase items as if they were adopted.
- Speaker labels in notes are text; do not infer identities.
