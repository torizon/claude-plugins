---
name: torizon-pre-push-triage
description: "Pre-push triage for commits in the Torizon/Toradex Yocto layers (meta-torizon, meta-torizon-bsp, meta-toradex-torizon, other meta-toradex-* layers) and toradex-manifest. Runs deterministic checks (72 columns, Signed-off-by, Related-to, AI attribution, secrets, manifest pins, Upstream-Status, whitespace, shellcheck), a commit-message review agent, a layer review agent and a bitbake parse, then stamps HEAD so the git pre-push hook lets the push through; after the PR/MR is opened as a draft, runs an independent review of it and posts a 'review done' comment. Use before git push or opening a PR/MR in these repos, right after opening one, when asked 'can I push these commits?', 'triage before push', 'review my commits before pushing', 'pre-push', 'pre-MR' (or in Portuguese: 'posso subir esses commits?', 'faz a triagem antes do push', 'revisa meus commits antes de subir'), or when a push is refused with 'pre-push triage'."
---

# Pre-push triage: Torizon layers and toradex-manifest

The pipeline a set of local commits goes through before it goes up as a
PR/MR. It works the same in a cloud session and on a local machine, on
either forge (GitHub or GitLab). Steps 0 to 4 work only on the local git
history, between the base and HEAD. Step 5 runs after the PR/MR has been
opened as a draft: an independent review of what is on the forge, and a
comment recording that it was done. To review someone else's PR/MR, use
`torizon-pr-reviewer` instead.

`SKILL_DIR` below is the directory of this file. The script is
`$SKILL_DIR/scripts/pre-mr.py`; always call it with `-C <repo>`.

Talk to the person in their language; everything posted on the forge is in
English.

## 0. Repository and gate

```bash
REPO="$(git -C <path> rev-parse --show-toplevel)"
PRE_MR="$SKILL_DIR/scripts/pre-mr.py"
python3 "$PRE_MR" -C "$REPO" status
```

If `status` says `pre-push hook: not installed`, tell the person that the
gate can block `git push` of untriaged commits in this clone, and offer to
install it:

```bash
python3 "$PRE_MR" -C "$REPO" install
```

`install` copies the script to `$(git rev-parse --git-common-dir)/pre-mr/`
and writes `.git/hooks/pre-push`. Nothing goes into the work tree or into a
commit. Install only if the person agrees. `uninstall` undoes it. With the
gate installed, run `install` again whenever this skill is updated, to
refresh the copy.

## 1. Scope

```bash
python3 "$PRE_MR" -C "$REPO" range
```

- With 0 commits there is nothing to review: stop.
- If the base is wrong (a fork whose `origin/master` lags upstream, a
  release branch such as `scarthgap-7.x.y`), set it with
  `git -C "$REPO" config pre-mr.base <ref>` and run `range` again. When in
  doubt about the base, ask instead of guessing.

## 2. The four steps, in parallel

Launch all four in a single message:

- **Bash:** `python3 "$PRE_MR" -C "$REPO" static`
- **Bash:** `python3 "$PRE_MR" -C "$REPO" bitbake`. Use `run_in_background`
  when `pre-mr.targets` is set, because a build can take hours. A parse
  alone takes a few minutes.
- **Agent** (general-purpose, read-only): the prompt is the content of
  `$SKILL_DIR/references/commit-msg-reviewer.md`, followed by the repo
  path, the `$PRE_MR` path and the commit list from step 1.
- **Agent** (general-purpose, read-only): the prompt is the content of
  `$SKILL_DIR/references/layer-reviewer.md`, followed by the repo path, the
  `$PRE_MR` path, the merge-base, the commit list and the `warn` lines from
  `static` once they exist. If `static` has not finished yet, wait for it
  and pass the `warn` lines in a follow-up message.

Say in both agent prompts: "Do not change files, commits or branches."

Each step ends with one verdict line:

- `static: PASS|FAIL`
- `bitbake: PASS|SKIP|FAIL`
- `commit-msg: PASS|FAIL`
- `review: PASS|FAIL`

The script prints `bitbake: SKIP` in three cases only: a cloud session, a
clone with `pre-mr.builddir=none`, or a repo that is not a layer (for
example toradex-manifest). Copy that line from the script's output; never
write it yourself.

On a local machine, bitbake needs a build directory that uses this clone:

```bash
git -C "$REPO" config pre-mr.builddir /path/to/build
```

Optionally, `pre-mr.init` points at `oe-init-build-env` and `pre-mr.targets`
sets build targets instead of the parse. Worktrees of the same repository
share this configuration (it lives in the common `.git`): if you point it at
another build directory for one worktree, set it back afterwards.

## 3. Report and stamp

Write the report to
`$(git -C "$REPO" rev-parse --git-common-dir)/pre-mr/report.md`:

1. the four verdict lines first, copied exactly as each step printed them;
2. then each step's findings, under its own heading.

If all four passed, or the only SKIP is bitbake's:

```bash
python3 "$PRE_MR" -C "$REPO" stamp "$(git -C "$REPO" rev-parse --git-common-dir)/pre-mr/report.md"
```

The stamp is valid for the exact HEAD hash. After an amend or a rebase, run
the pipeline again.

## 4. What to show the person

Start with the verdict in one line: **ready to push** (stamped) or **do not
push yet**. Then:

1. the blockers, each with file and line, or with the commit and the
   offending line;
2. the messages suggested by the commit reviewer, ready to use;
3. the non-blocking suggestions, kept apart from the blockers;
4. what did not run and why (bitbake SKIP in the cloud, shellcheck or
   oelint-adv not installed), with the command for the person to run
   locally.

Do not amend, rebase or edit anything to fix a finding unless the person
asks. If they ask you to fix messages, also follow the `torizon-commit-msg`
skill when it is available. After any fix, run the whole pipeline again.

## 5. After the PR/MR is opened as a draft

This skill does not push. When the person asks for the push and for the
PR/MR to be opened (always as a draft), the triage continues on the forge:

1. **Independent review of the opened PR/MR.** Launch an Agent
   (general-purpose, read-only) with the content of
   `$SKILL_DIR/references/pr-reviewer.md`, followed by the PR/MR URL, the
   path of the worktree with the exact branch, and the build directory if
   there is one. Do not pass the triage's conclusions: the value of this
   review is that it is not anchored on them. For risky changes (OTA,
   U-Boot, secure boot, aktualizr), launch two reviewers with different
   focuses (runtime behaviour; packaging and integration).
2. **Verify every finding yourself** before using it: run the command,
   read the line. A reviewer can be wrong (for example, reading only the
   `defconfig` and missing the fragment the recipe applies). Drop what does
   not hold up, and say why.
3. **No blocker:** post a comment on the PR/MR, in English, saying the
   review was done and what it checked, even when there is nothing to fix:
   `Independent review done; nothing blocking. <what was checked>.`
   followed by the non-blocking notes and what is still untested.
   - GitHub: `gh api -X POST repos/<owner>/<repo>/issues/<n>/comments -F body=@<file>`.
   - GitLab: `glab api -X POST projects/<id>/merge_requests/<iid>/notes -F body=@<file>`.
   - It is a comment, never an APPROVE: the PR is the person's own.
4. **Blocker found:** post nothing yet. Show the blockers to the person and
   fix only if they ask; after any fix, run the whole pipeline again (steps
   1 to 3) and only then repeat this section.
5. If the review shows the **description** claims something the evidence
   does not support (a test that was not run that way, an inaccurate list
   of machines), propose the description fix to the person.

After a force-push (amend, rebase), leave a short note on the PR/MR saying
what changed and that the triage was run again, for anyone who had already
read it.

## Rules

- The verdict line in the report is the one the step printed. Never write
  PASS for a step that failed, did not run or was interrupted.
- This skill does not push. Push only when the person asks, and always with
  `git push`, so the hook runs. Never use `--no-verify`, never change
  `core.hooksPath` and never remove the hook to get past the gate.
- Never push commits to these repos with tools that write straight to the
  remote (for example `mcp__github__push_files` or
  `create_or_update_file`): they skip the hook.
- A secret finding is treated as real until the person confirms otherwise.
  Do not repeat the value in the chat; cite the file and line.
- The stamp does not replace human review of the PR/MR: it is the minimum
  the commits meet before someone reads them.
