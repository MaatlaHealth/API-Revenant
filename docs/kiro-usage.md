# Kiro Usage Record

This file separates two different things so that nothing here overstates how Kiro was used.

## 1. Kiro project configuration that was generated outside Kiro

The contents of `.kiro/` (specs, steering, agents, hooks) were authored on 2026-11-24 with Claude Code, **not inside the Kiro IDE or CLI**. They follow the formats described in the official Kiro documentation at the time of writing:

| Item | Format source | Verified in an installed Kiro? |
|---|---|---|
| `.kiro/steering/*.md` | kiro.dev/docs/steering — YAML front matter with `inclusion`, `fileMatchPattern` | No |
| `.kiro/specs/api-revenant/{requirements,design,tasks}.md` | kiro.dev/docs/specs — EARS requirements, design, tasks | No |
| `.kiro/agents/*.md` | kiro.dev/docs/custom-agents — Markdown with YAML front matter | No |
| `.kiro/hooks/*.json` | kiro.dev/docs/hooks — `version`, `hooks[]`, `trigger`, `matcher`, `action` | No |

Details that the documentation pages consulted did **not** confirm, and which should be checked the first time the project is opened in Kiro:

- The spec directory path `.kiro/specs/<name>/` (used by convention).
- The `tasks.md` checkbox syntax and the `_Requirements: …_` reference style.
- The identifier for the file-save hook trigger. The documentation's own example uses `PostFileSave`; the trigger reference lists it only by display name.

Kiro was not installed on the machine where these files were written, so none of them has been loaded by Kiro yet.

## 2. Work actually performed inside Kiro

Record real Kiro sessions here as they happen. Do not add entries for work done with other tools.

| Date | Kiro surface (IDE/CLI) and version | Spec task or prompt | What Kiro produced | What was kept or changed by hand | Commit |
|---|---|---|---|---|---|
| | | | | | |

## 3. First-run checklist in Kiro

- [ ] Open the repository in Kiro and confirm the three steering foundation files load.
- [ ] Confirm the `api-revenant` spec appears and its tasks are recognised; fix syntax if not, and note the change above.
- [ ] Confirm the three custom agents are selectable.
- [ ] Confirm the hooks load; correct the trigger identifier if Kiro rejects it.
- [ ] Record the Kiro version used.

## 4. MCP

No MCP integration is implemented or claimed.
