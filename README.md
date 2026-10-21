# agentic-skills-marketplace
Marketplace repository for personal agentic skills

## Installation

### Claude Code plugin marketplace

In Claude Code, add the marketplace and install a plugin from it:

```
/plugin marketplace add matthewfeickert/agentic-skills-marketplace
/plugin install conda-forge-pr-rebase@matthewfeickert-agentic-skills-marketplace
```

### GitHub CLI

The skills follow the [Agent Skills specification](https://agentskills.io/specification), so they can also be installed with the GitHub CLI's [`gh skill`](https://cli.github.com/manual/gh_skill) command (in preview as of `gh` `v2.102`). To install a skill for Claude Code at user scope (`~/.claude/skills/`):

```bash
gh skill install matthewfeickert/agentic-skills-marketplace conda-forge-pr-rebase --agent claude-code --scope user
```

Use `--scope project` to install into the current repository instead, and `gh skill update --all` to update installed skills later. Running `gh skill install matthewfeickert/agentic-skills-marketplace` without a skill name prompts for one to install (or lists the available skills when run non-interactively).

The skills reference their bundled scripts through `${CLAUDE_SKILL_DIR}`, which only Claude Code expands. With other `--agent` values, the agent must resolve the script paths relative to the skill's installed directory.

## Plugins

| Plugin | Description |
| --- | --- |
| [`conda-forge-pr-rebase`](plugins/conda-forge-pr-rebase) | Rebase an open conda-forge feedstock PR (typically a `regro-cf-autotick-bot` version bump) onto upstream `main`, validate its run and host requirements against the package metadata, regenerate with `conda-smithy`, and force-push to the PR branch. Requires `gh`, `pixi`, `conda-smithy`, and push access to the PR's head branch. |
