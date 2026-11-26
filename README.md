# Torizon Claude plugins

A Claude Code plugin marketplace with the Torizon team's plugins. Private to the `torizon` GitHub organization.

| Plugin | What it does |
|---|---|
| `torizon-pre-push-triage` (in this repo) | Before a push to the Torizon Yocto layers (meta-torizon, meta-torizon-bsp, meta-toradex-torizon, other meta-toradex-*) or toradex-manifest: deterministic checks (72 columns, Signed-off-by, Related-to, AI attribution, secrets, manifest pins, Upstream-Status, whitespace, shellcheck, oelint-adv), a commit-message review agent, a layer review agent and a bitbake parse, then a stamp that an optional git pre-push hook checks. After the PR/MR is opened as a draft: an independent review of it and a "review done" comment. |
| `bitbake-watch` ([repo](https://github.com/MatheusRodrigues-tor/bitbake-watch)) | Live BitBake build progress above the prompt: follows the running build, or a build dir or log given to `/bbwatch`. |
| `pending-board` ([repo](https://github.com/MatheusRodrigues-tor/pending-board)) | A pane of pending work items (tickets, PRs, follow-ups) that Claude keeps current, with a monitor tab for the GitHub PRs and GitLab MRs behind them. |

`bitbake-watch` and `pending-board` are listed here from their own repositories, so they are developed there.

## Install (Claude Code)

The repository is private to the `torizon` organization, so git must already
be able to clone it (`gh auth login`, or an SSH key on your GitHub account).

```
/plugin marketplace add torizon/claude-plugins
/plugin install torizon-pre-push-triage@torizon
/plugin install bitbake-watch@torizon
/plugin install pending-board@torizon
```

`/plugin marketplace update torizon` pulls new versions.

To use the skill on claude.ai instead, zip the folder
`plugins/torizon-pre-push-triage/skills/torizon-pre-push-triage` and upload
it under Settings > Capabilities > Skills.

## Using the triage

Ask Claude Code, from inside the clone you want to push: "triage these
commits before I push", "pre-push", or "can I push these commits?".

Requirements:
- `python3` and `git`;
- `gh` (GitHub) or `glab` (GitLab), signed in, for the PR/MR step;
- optional: `shellcheck` and `oelint-adv`; without them those checks are
  reported as SKIP.

Per clone (stored in the clone's git config):

```bash
git config pre-mr.base repo/wrynose-8.x.y          # base to compare against, if the default guess is wrong
git config pre-mr.builddir /path/to/build          # a Yocto build dir whose bblayers.conf uses this clone
git config pre-mr.init /path/to/oe-init-build-env  # only if it is not found from the build dir
git config pre-mr.targets "linux-imx"              # optional: build targets instead of a parse
```

The skill offers to install the pre-push hook (`pre-mr.py install`); with it,
`git push` refuses commits that have no stamp. Nothing is written to the work
tree.

## Changing a skill

For `torizon-pre-push-triage`, open a pull request here and bump `version`
in both `.claude-plugin/marketplace.json` and the plugin's
`.claude-plugin/plugin.json`. For the others, change them in their own
repositories and bump the `version` listed here.
