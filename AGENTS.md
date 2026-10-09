## Agent skills

### Issue tracker

Issues live in GitHub Issues on `mmcmd/Discord-RSS-Bot`, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Commands

Adding, renaming or removing a slash command, or changing its options, means updating its entry in `src/rssbot/commands/help.py`. See `src/rssbot/commands/PATTERNS.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
