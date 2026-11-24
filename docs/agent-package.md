# Connecting a coding agent to PrometheOS

PrometheOS works with any agent that speaks MCP. All agents get the same tools, and none of them gets an operation of its own. Two hosts are packaged.

Each setup works the same way:
- it shows its plan first;
- it writes files only when you add `--apply`;
- it writes only into the folder you name;
- it never changes an agent's global settings.

The examples assume that `prometheos` runs from your PrometheOS environment. If it is not on PATH, use `<env>\Scripts\prometheos.exe`.

## Claude Code: the PrometheOS plugin

```
prometheos host setup --host claude-code-plugin --target C:\Users\you\PrometheOS-plugin --project <project id>
prometheos host setup --host claude-code-plugin --target C:\Users\you\PrometheOS-plugin --project <project id> --apply
```

The plugin is a local marketplace with one plugin, `prometheos`. It contains:
- the PrometheOS MCP server for that project;
- the bundled skills (five: adopt-folder, capture-research-evidence, inspect-claim-evidence, reconcile-meeting, work-items);
- a session-start hook.

Then, in Claude Code (opened in the folder where you code):

1. `/plugin marketplace add C:\Users\you\PrometheOS-plugin`
2. `/plugin install prometheos@prometheos-local`. Choose the project scope to keep it in this folder.
3. Start a new session.

At the start of a session, the hook gives the agent a short read-only summary:
- your open work items, by deadline, in your project's time zone;
- items an agent suggested, which wait for your adoption;
- research events that need attention.

The agent is told to mention them briefly and ask you one question: whether you want to work on one now, and which. It must never start anything on its own, or because of a deadline. The hook only reads. If PrometheOS cannot answer, the session starts normally without the summary.

You can see the same summary yourself:

```
prometheos agent session-start --project <project id>
```

## OpenCode

```
prometheos host setup --host opencode --target <your coding folder> --project <project id> --apply
```

This writes, inside the coding folder:
- `opencode.json`: the `prometheos` MCP server, and an `instructions` entry for the session-start file. Existing settings are kept.
- `.opencode/skills/`: the bundled skills.
- `.opencode/prometheos-session-start.md`: asks the agent to read `work_queue` at the start of a session, list the open items and ask you.

The project-local config uses the plain `prometheos` command, because such files may be shared. Put your PrometheOS environment's `Scripts` folder on PATH, or edit the command in `opencode.json`.

## What the agent can and cannot do

The agent works with agent scope. Protected decisions answer `approval_required` with a request that waits in the hub's Approvals. These include:
- acknowledging events;
- adopting suggested items;
- corrections, installs and pushes.

You grant or deny each request there. The agent then resumes the granted request once.
