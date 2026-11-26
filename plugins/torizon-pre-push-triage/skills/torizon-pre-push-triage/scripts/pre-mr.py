#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pre-push gate for the Torizon / Toradex Yocto layers and manifests.

Part of the torizon-pre-push-triage skill. The commit-message and layer
reviews run as Claude Code agents from that skill; this script holds the
steps that need no model: the deterministic checks, the bitbake check, the
pass stamps and the git pre-push hook that refuses a push without a stamp.

Run it from inside the clone being pushed (or pass -C <path>).

Commands:
  range [REV]        base branch and the commits under review
  static [REV]       message format, trailers, secrets, manifests, patches,
                     whitespace, shellcheck, oelint-adv
  bitbake            bitbake -p (or the configured targets) on the clean HEAD
  stamp REPORT       record REPORT as the pass for HEAD
  status             whether HEAD has a pass
  install            copy this script into the clone's git dir and install
                     the pre-push hook (nothing is written to the work tree)
  uninstall          remove the hook and the copy
  pre-push           git pre-push hook entry point

Configuration (environment variable, or git config in the clone):
  PRE_MR_BASE      / pre-mr.base      base ref; default upstream/ or origin/
                                      HEAD, master or main, first that exists
  PRE_MR_BUILDDIR  / pre-mr.builddir  Yocto build directory, or "none"
  PRE_MR_INIT      / pre-mr.init      oe-init-build-env; found from builddir
  PRE_MR_TARGETS   / pre-mr.targets   bitbake targets; default: parse only
"""

import argparse
import collections
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = None  # top of the work tree under review, set in main()
MAX_COLUMNS = 72
URL_REFERENCE = re.compile(r"^\[\d+\] \S+://\S+$")
IDENTITY_TRAILER = re.compile(r"^[A-Z][\w-]*-by: [^<>]+ <[^<>\s]+>$", re.I)
TICKET = r"(?:[A-Z][A-Z0-9]*-\d+|#\d+)"
RELATED = re.compile(rf"^Related-to: {TICKET}(?:, {TICKET})*$")
BASE_CANDIDATES = (
    "upstream/HEAD", "upstream/master", "upstream/main",
    "origin/HEAD", "origin/master", "origin/main",
)
STEPS = ("static", "commit-msg", "review", "bitbake")
VERDICT = re.compile(r"^(%s):\s*(PASS|SKIP|FAIL)\s*$" % "|".join(STEPS), re.M)
ZERO_SHA = re.compile(r"^0+$")
RECIPE_SUFFIXES = (".bb", ".bbappend", ".bbclass", ".inc")
INIT_CANDIDATES = (
    "sources/poky/oe-init-build-env",
    "layers/poky/oe-init-build-env",
    "layers/openembedded-core/oe-init-build-env",
    "sources/openembedded-core/oe-init-build-env",
)
HOOK_MARK = "torizon-pre-push-triage gate"
INSTALLED_HOOK = f"""#!/bin/sh
# {HOOK_MARK}: refuse commits that have not passed the pre-push triage.
# Installed by: python3 <skill>/scripts/pre-mr.py install
gate="$(git rev-parse --git-common-dir)/pre-mr/pre-mr.py"
[ -f "$gate" ] || exit 0
exec python3 "$gate" pre-push "$@"
"""

# Attribution to an AI or a bot in a commit message.
AI_TRAILER = re.compile(
    r"^(Co-Authored-By|Co-developed-by|Reported-by|Suggested-by|Tested-by|"
    r"Reviewed-by|Acked-by|Helped-by|Assisted-by):.*"
    r"\b(claude|anthropic|copilot|chatgpt|openai|gpt-\d|bot)\b", re.I)
AI_LINE = re.compile(r"(Claude-Session:|Generated with \[?Claude|"
                     r"claude\.ai/code|noreply@anthropic\.com)", re.I)
PROCESS = re.compile(
    r"\b(I ran|we ran|after (investigating|grepping|digging)|I noticed|"
    r"while reviewing|found (this|it) (while|when)|as discussed|"
    r"this session|rodei|investiguei|percebi)\b", re.I)

# Secrets: a hit fails the step.
SECRETS = (
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("GitHub token", re.compile(
        r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_\w{40,}")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("JWT", re.compile(r"\beyJ[\w-]{10,}\.eyJ[\w-]{10,}\.[\w-]+")),
    ("credentials in a URL", re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^/\s:@'\"<>]+:[^/\s@'\"<>${}]{3,}@")),
    ("NVD API key", re.compile(
        r"NVDCVE_API_KEY\s*[?:+.]*=\s*['\"]?[0-9a-fA-F-]{20,}")),
)
# Worth a look, judged by the layer review: warn only.
SECRET_LIKE = re.compile(
    r"(?i)\b\w*(password|passwd|secret|api_?key|token|private_?key)\w*"
    r"\s*[?:+.]*=\s*['\"][^'\"$\s]{8,}['\"]")
INTERNAL = re.compile(
    r"(?i)\b[\w.-]+\.(internal|intra|corp|lan)\b|"
    r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
INCIDENT = re.compile(
    r"(?i)\b(leak(ed|s)?|vazad[oa]s?|exposed|exposure|compromised|"
    r"(real|actual|production) (key|token|secret|credential)s?|"
    r"gitleaks (stage )?(was )?(removed|disabled))\b")
AUTOREV = re.compile(r"\bAUTOREV\b")
XML_REVISION = re.compile(r'\brevision="([^"]+)"')
PINNED = re.compile(r"^([0-9a-f]{40}|refs/tags/\S+|v?\d+(\.\d+)+([.+~-][\w.]+)?)$")
UPSTREAM_STATUS = re.compile(
    r"^Upstream-Status:\s*(Pending|Submitted|Accepted|Backport|Denied|"
    r"Inappropriate|Inactive-Upstream)\b", re.M)


def die(message):
    sys.exit(f"pre-mr: {message}")


def run_git(*args, root=None):
    return subprocess.run(["git", "-c", "core.quotePath=false",
                           "-C", str(root or ROOT), *args], text=True,
                          encoding="utf-8", errors="replace",
                          capture_output=True)


def git(*args, root=None):
    result = run_git(*args, root=root)
    if result.returncode != 0:
        die(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.rstrip("\n")


def setting(env_name, config_name):
    value = os.environ.get(env_name)
    if value:
        return value
    result = run_git("config", "--get", config_name)
    return result.stdout.strip() if result.returncode == 0 else ""


def in_cloud_session():
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true"


def git_common_dir():
    common = Path(git("rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = ROOT / common
    return common.resolve()


def stamp_dir():
    return git_common_dir() / "pre-mr" / "stamps"


def resolve_base():
    configured = setting("PRE_MR_BASE", "pre-mr.base")
    for ref in [configured] if configured else BASE_CANDIDATES:
        if run_git("rev-parse", "-q", "--verify",
                   f"{ref}^{{commit}}").returncode == 0:
            return ref
    if configured:
        die(f"base {configured!r} does not resolve to a commit")
    die("no base branch found; set one with: git config pre-mr.base <ref>")


def review_range(rev="HEAD"):
    """Return (base, merge-base, commits oldest first, merge commits)."""
    base = resolve_base()
    result = run_git("merge-base", base, rev)
    if result.returncode != 0:
        shallow = run_git("rev-parse",
                          "--is-shallow-repository").stdout.strip() == "true"
        die(f"no merge-base between {base} and {rev}"
            + ("; this clone is shallow, run: git fetch --unshallow"
               if shallow else ""))
    merge_base = result.stdout.strip()
    commits = git("rev-list", "--reverse", "--no-merges",
                  f"{merge_base}..{rev}").split()
    merges = git("rev-list", "--merges", f"{merge_base}..{rev}").split()
    return base, merge_base, commits, merges


class Results:
    def __init__(self):
        self.failures = 0

    def ok(self, message):
        print(f"ok    {message}")

    def skip(self, message):
        print(f"SKIP  {message}")

    def fail(self, message, details=()):
        self.failures += 1
        print(f"FAIL  {message}")
        for detail in details:
            print(f"        {detail}")

    def warn(self, message, details=()):
        print(f"warn  {message}")
        for detail in details:
            print(f"        {detail}")


# --- commit messages -------------------------------------------------------

def message_problems(commit):
    """Return (subject, blocking problems, warnings)."""
    message = git("log", "-1", "--format=%B", commit).rstrip("\n")
    author_email = git("log", "-1", "--format=%ae", commit)
    lines = message.split("\n")
    subject = lines[0]
    problems, warnings = [], []

    if not subject.strip():
        problems.append("empty subject")
    if (re.match(r"(fixup|squash|amend)! ", subject)
            or re.search(r"\bWIP\b", subject, re.I)):
        problems.append("work-in-progress commit")
    if len(lines) > 1 and lines[1].strip():
        problems.append("the line after the subject must be blank")
    if subject.rstrip().endswith("."):
        warnings.append("subject ends with a period")

    for number, line in enumerate(lines, 1):
        # Prose wraps at 72. What cannot wrap is exempt: a reference link,
        # a line carrying a URL, a name-and-address trailer, and quoted
        # output, which the message indents.
        unwrappable = (URL_REFERENCE.match(line)
                       or IDENTITY_TRAILER.match(line)
                       or line[:1] in (" ", "\t")
                       or len(re.sub(r"\S+://\S+", "", line)) <= MAX_COLUMNS)
        if len(line) > MAX_COLUMNS and (number == 1 or not unwrappable):
            problems.append(f"line {number} has {len(line)} columns "
                            f"(max {MAX_COLUMNS}): {line[:50]}...")
        if AI_TRAILER.search(line) or AI_LINE.search(line):
            problems.append(f"attribution to an AI or a bot: {line}")
        if PROCESS.search(line):
            warnings.append(f"may narrate the work process: {line}")
        for name, pattern in SECRETS:
            if pattern.search(line):
                problems.append(f"possible secret ({name}) in the message")
        if INCIDENT.search(line):
            warnings.append(f"incident detail in a public history? {line}")

    # A cherry-pick with -x -s legitimately signs off again after the
    # "(cherry picked from ...)" line, so trailers repeat only within one
    # segment.
    segment, repeated = [], set()
    for line in lines + ["(cherry picked from"]:
        if line.startswith("(cherry picked from"):
            repeated |= {l for l in segment if segment.count(l) > 1}
            segment = []
        elif re.match(r"[\w-]+: \S", line):
            segment.append(line)
    for line in sorted(repeated):
        problems.append(f"repeated trailer: {line}")

    signoffs = [l for l in lines if l.startswith("Signed-off-by:")]
    if not signoffs:
        problems.append("missing Signed-off-by")
    elif not any(f"<{author_email.lower()}>" in l.lower() for l in signoffs):
        problems.append(f"no Signed-off-by from the author <{author_email}>")

    for line in lines:
        if (re.match(r"(related[ _-]?to|relates[ _-]to|refs?|references)\s*:",
                     line, re.I)
                and not line.startswith("Related-to: ")):
            problems.append(f"write the ticket trailer as 'Related-to: "
                            f"TOR-1234', not: {line}")
        elif line.startswith("Related-to: ") and not RELATED.match(line):
            problems.append(f"Related-to takes real tickets, comma-separated "
                            f"(TOR-1234, #12): {line}")

    def first(prefix):
        return next((i for i, l in enumerate(lines) if l.startswith(prefix)),
                    None)

    related, signed = first("Related-to:"), first("Signed-off-by:")
    if related is not None and signed is not None and related > signed:
        problems.append("Related-to must come before Signed-off-by")
    trailers = [i for i in (related, signed) if i is not None]
    if trailers and any(URL_REFERENCE.match(l)
                        for l in lines[min(trailers):]):
        problems.append("[n] reference links go before the trailers")

    return subject, problems, warnings


# --- diff content ------------------------------------------------------------

def added_lines(merge_base, rev):
    """{path: [(line number, text)]} for lines added over the merge-base."""
    out = run_git("diff", "--no-color", "--no-ext-diff", "-U0", "-M",
                  merge_base, rev).stdout
    files, path, number = {}, None, 0
    for line in out.split("\n"):
        if line.startswith("+++ "):
            path = None if line == "+++ /dev/null" else line[6:]
            if path:
                files.setdefault(path, [])
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            number = int(match.group(1)) if match else 0
        elif path and line.startswith("+"):
            files[path].append((number, line[1:]))
            number += 1
    return files


def removed_lines(merge_base, rev, path, old_path):
    out = run_git("diff", "--no-color", "-U0", merge_base, rev, "--",
                  old_path, path).stdout
    return [l[1:] for l in out.split("\n")
            if l.startswith("-") and not l.startswith("---")]


def check_content(results, merge_base, rev, changed):
    added = added_lines(merge_base, rev)
    secrets, notes = [], collections.defaultdict(list)
    for path, lines in added.items():
        for number, text in lines:
            where = f"{path}:{number}: {text.strip()[:100]}"
            # A secret is reported by place only, so the report and the
            # chat never carry the value.
            hits = [name for name, pattern in SECRETS if pattern.search(text)]
            for name in hits:
                secrets.append(f"{name}: {path}:{number} (value not shown)")
            if hits:
                continue
            if SECRET_LIKE.search(text):
                notes["value assigned to a secret-looking name"].append(
                    f"{path}:{number} (value not shown)")
            if INTERNAL.search(text):
                notes["internal host or private IP address"].append(where)
            if INCIDENT.search(text):
                notes["incident detail in a public repository"].append(where)
            if AUTOREV.search(text):
                notes["AUTOREV (floating source revision)"].append(where)
    if secrets:
        results.fail("possible secrets in added lines", secrets)
    else:
        results.ok("secrets")
    for label, wheres in notes.items():
        results.warn(label, wheres[:20])

    # Manifests: a release manifest pins every revision; any manifest moving
    # a project from a pin to a branch needs a reason the review can see.
    for path, old_path in changed:
        if not path.endswith(".xml"):
            continue
        new = [t for _, t in added.get(path, [])]
        if not any(XML_REVISION.search(t) for t in new):
            continue
        floating = [t.strip()[:120] for t in new
                    for r in XML_REVISION.findall(t) if not PINNED.match(r)]
        old_pins = [r for t in removed_lines(merge_base, rev, path, old_path)
                    for r in XML_REVISION.findall(t) if PINNED.match(r)]
        release = Path(path).name == "default.xml"
        if floating and release:
            results.fail(f"manifest {path}: release manifest with a "
                         f"floating revision", floating)
        elif floating and old_pins:
            results.warn(f"manifest {path}: pinned revision(s) replaced by "
                         f"a branch; the commit must say why", floating)
        elif floating:
            results.warn(f"manifest {path}: floating revision(s) (fine in a "
                         f"development manifest)", floating)
        else:
            results.ok(f"manifest {path}: revisions pinned")

    # New patches need Upstream-Status (OE patch-status QA).
    status = run_git("diff", "--name-status", "--diff-filter=A", merge_base,
                     rev).stdout.split("\n")
    new_paths = {l.split("\t", 1)[1] for l in status if "\t" in l}
    for path in sorted(new_paths):
        if not path.endswith((".patch", ".diff")):
            continue
        content = (blob(rev, path) or b"").decode("utf-8", "replace")
        header = content.split("\n---\n", 1)[0]
        if not UPSTREAM_STATUS.search(header):
            results.fail(f"patch {path}: no valid Upstream-Status line")
        elif not re.search(r"^Signed-off-by:", header, re.M):
            results.warn(f"patch {path}: no Signed-off-by in the header")
        else:
            results.ok(f"patch {path}")
    for path in sorted(new_paths):
        if path.endswith(".bbappend"):
            results.warn(f"new bbappend {path}: check that its name and "
                         f"version wildcard match the target recipe")


def whitespace_problems(merge_base, rev):
    """git diff --check, except Markdown hard line breaks (two spaces)."""
    result = run_git("diff", "--check", merge_base, rev, "--", ".",
                     ":(exclude)*.patch", ":(exclude)*.diff")
    if result.returncode == 0:
        return []
    lines = result.stdout.rstrip("\n").split("\n")
    problems, i = [], 0
    while i < len(lines):
        head = lines[i]
        body = lines[i + 1] if i + 1 < len(lines) else ""
        i += 2 if body.startswith("+") else 1
        match = re.match(r"^(.*):\d+: trailing whitespace\.$", head)
        if (match and match.group(1).endswith((".md", ".markdown"))
                and re.search(r"\S  $", body)):
            continue
        problems.append(head)
        if body.startswith("+"):
            problems.append(body)
    return problems


# --- linters, compared against the base so only new findings count ----------

def blob(rev, path):
    """File content at rev as bytes, so linters see it unchanged."""
    result = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:{path}"],
                            capture_output=True)
    return result.stdout if result.returncode == 0 else None


def is_shell(path, content):
    if path.endswith((".sh", ".bash")):
        return True
    first = content.split(b"\n", 1)[0].decode("utf-8", "replace") \
        if content else ""
    return bool(re.match(r"#!\s*\S*/(env\s+)?(ba|da)?sh\b", first))


def lint_findings(command, filename, content, parse):
    """Run a linter on content saved as filename; return finding keys."""
    if content is None:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / filename
        target.write_bytes(content)
        result = subprocess.run([*command, str(target)], text=True,
                                encoding="utf-8", errors="replace",
                                capture_output=True, cwd=tmp)
    findings = []
    for line in (result.stdout + result.stderr).splitlines():
        parsed = parse(line)
        if parsed:
            findings.append(parsed)
    return findings


def parse_shellcheck(line):
    # gcc format: path:line:col: severity: message [SCxxxx]
    match = re.match(r"^.*?:(\d+):\d+: (\w+): (.*)$", line)
    return (match.group(3), f"line {match.group(1)}: {match.group(2)}: "
            f"{match.group(3)}") if match else None


def parse_oelint(line):
    # default format: path:line:severity:id:message
    match = re.match(r"^.*?:(\d+):(error|warning):(.*)$", line)
    return (match.group(3), f"line {match.group(1)}: {match.group(2)}: "
            f"{match.group(3)}") if match else None


def new_findings(command, path, old_path, merge_base, rev, parse):
    filename = Path(path).name
    head = lint_findings(command, filename, blob(rev, path), parse)
    old = blob(merge_base, old_path)
    known = collections.Counter(
        key for key, _ in lint_findings(command, filename, old, parse)
    ) if old is not None else collections.Counter()
    fresh = []
    for key, shown in head:
        if known[key]:
            known[key] -= 1
        else:
            fresh.append(shown)
    return fresh


def lint(results, name, tool, command, paths, merge_base, rev, parse,
         blocking=True):
    """Lint (path, path at merge-base) pairs; only added findings count."""
    if not paths:
        results.ok(f"{name}: no matching files changed")
        return
    if not shutil.which(tool):
        results.skip(f"{name}: {tool} is not installed "
                     f"({len(paths)} file(s) not checked)")
        return
    for path, old_path in paths:
        fresh = new_findings(command, path, old_path, merge_base, rev, parse)
        if not fresh:
            results.ok(f"{name}: {path}")
        elif blocking:
            results.fail(f"{name}: {path}: {len(fresh)} new finding(s)",
                         fresh)
        else:
            results.warn(f"{name}: {path}: {len(fresh)} new finding(s)",
                         fresh)


def changed_files(merge_base, rev):
    """(path at rev, path at merge-base) for added, copied, modified and
    renamed files; a rename is linted against the file it came from."""
    out = run_git("diff", "-z", "-M", "--name-status", "--diff-filter=ACMR",
                  merge_base, rev).stdout.split("\0")
    pairs, i = [], 0
    while i < len(out) and out[i]:
        status = out[i]
        if status[0] in "RC":
            pairs.append((out[i + 2], out[i + 1]))
            i += 3
        else:
            pairs.append((out[i + 1], out[i + 1]))
            i += 2
    return pairs


def run_static(rev="HEAD"):
    results = Results()
    base, merge_base, commits, merges = review_range(rev)
    if not commits and not merges:
        results.ok(f"no commits between {base} and {rev}")
        return results

    if merges:
        # A merge can carry changes of its own (a conflict resolution) that
        # no message check or per-commit review sees; the layers rebase.
        results.fail("merge commit(s) in the range; rebase onto the base",
                     [git("log", "-1", "--format=%h %s", m) for m in merges])

    for commit in commits:
        subject, problems, warnings = message_problems(commit)
        label = f"{git('rev-parse', '--short', commit)} {subject[:60]}"
        if problems:
            results.fail(f"message: {label}", problems)
        else:
            results.ok(f"message: {label}")
        if warnings:
            results.warn(f"message: {label}", warnings)
        files = git("show", "--name-only", "--format=", commit).split("\n")
        if len([f for f in files if f]) > 15:
            results.warn(f"{label}: {len(files)} files in one commit; check "
                         f"it is one logical change")

    whitespace = whitespace_problems(merge_base, rev)
    if whitespace:
        results.fail("whitespace errors in the diff", whitespace[:20])
    else:
        results.ok("whitespace")

    changed = changed_files(merge_base, rev)
    check_content(results, merge_base, rev, changed)

    shell = [(p, o) for p, o in changed if is_shell(p, blob(rev, p))]
    recipes = [(p, o) for p, o in changed if p.endswith(RECIPE_SUFFIXES)]
    lint(results, "shellcheck", "shellcheck",
         ["shellcheck", "-f", "gcc", "-S", "warning"],
         shell, merge_base, rev, parse_shellcheck)
    # oelint-adv sees one file without the rest of the layers, so includes,
    # machines and overrides from other layers read as errors: its findings
    # inform the review instead of failing the step.
    lint(results, "oelint-adv", "oelint-adv", ["oelint-adv", "--quiet"],
         recipes, merge_base, rev, parse_oelint, blocking=False)
    return results


# --- commands --------------------------------------------------------------

def cmd_range(args):
    base, merge_base, commits, merges = review_range(args.rev)
    print(f"repository: {ROOT}")
    print(f"remote:     {run_git('remote', 'get-url', 'origin').stdout.strip()}")
    print(f"branch:     {git('rev-parse', '--abbrev-ref', 'HEAD')}")
    print(f"base:       {base} (merge-base {merge_base[:12]})")
    print(f"commits:    {len(commits)}" +
          (f" (+{len(merges)} merge)" if merges else ""))
    if commits or merges:
        print(git("log", "--format=  %h %s", f"{merge_base}..{args.rev}"))
        print(git("diff", "--stat", merge_base, args.rev))
    if run_git("status", "--porcelain").stdout.strip():
        print("note:       uncommitted changes in the work tree are not "
              "reviewed and are not pushed")
    return 0


def cmd_static(args):
    results = run_static(args.rev)
    verdict = "FAIL" if results.failures else "PASS"
    print(f"static: {verdict}")
    return 1 if results.failures else 0


def bitbake_skip_reason():
    """Why bitbake does not apply to this clone, or None."""
    if in_cloud_session():
        return "cloud session, no Yocto build environment"
    if setting("PRE_MR_BUILDDIR", "pre-mr.builddir") == "none":
        return "disabled for this clone (pre-mr.builddir=none)"
    if not (ROOT / "conf" / "layer.conf").is_file():
        return "not a layer (no conf/layer.conf), e.g. a manifest repository"
    return None


def find_init(builddir):
    configured = setting("PRE_MR_INIT", "pre-mr.init")
    if configured:
        return Path(configured).expanduser()
    for candidate in INIT_CANDIDATES:
        path = builddir.parent / candidate
        if path.is_file():
            return path
    die(f"no oe-init-build-env next to {builddir}; "
        f"set it with: git config pre-mr.init <path>")


def in_build_env(init, builddir, command, capture=False):
    script = (f". {shlex.quote(str(init))} {shlex.quote(str(builddir))} "
              f">/dev/null && {command}")
    return subprocess.run(["bash", "-c", script], text=True,
                          capture_output=capture)


def cmd_bitbake(args):
    reason = bitbake_skip_reason()
    if reason:
        print(f"SKIP  bitbake: {reason}")
        print("bitbake: SKIP")
        return 0
    configured = setting("PRE_MR_BUILDDIR", "pre-mr.builddir")
    if not configured:
        print("FAIL  bitbake: no build directory configured; set one with\n"
              "        git config pre-mr.builddir <dir>   "
              "(or 'none' to opt out)")
        print("bitbake: FAIL")
        return 1
    if git("status", "--porcelain"):
        print("FAIL  bitbake: uncommitted or untracked files; the build "
              "would test them instead of HEAD")
        print("bitbake: FAIL")
        return 1

    builddir = Path(configured).expanduser().resolve()
    init = find_init(builddir)
    layers = in_build_env(init, builddir,
                          "bitbake-getvar --quiet --value BBLAYERS",
                          capture=True)
    if layers.returncode != 0:
        print(layers.stdout + layers.stderr)
        print("FAIL  bitbake: could not read BBLAYERS from the build directory")
        print("bitbake: FAIL")
        return 1
    paths = {Path(p).resolve() for p in layers.stdout.split()}
    if ROOT not in paths:
        print(f"FAIL  bitbake: {builddir} does not use this clone ({ROOT});\n"
              f"        add it to BBLAYERS or point pre-mr.builddir at a "
              f"build that does")
        print("bitbake: FAIL")
        return 1

    # A parse fails on a bbappend that matches no recipe, unless the build
    # sets BB_DANGLINGAPPENDS_WARNONLY.
    targets = setting("PRE_MR_TARGETS", "pre-mr.targets")
    command = f"bitbake -q {targets}" if targets else "bitbake -q -p"
    print(f"running in {builddir}: {command}", flush=True)
    result = in_build_env(init, builddir, command)
    verdict = "PASS" if result.returncode == 0 else "FAIL"
    print(f"bitbake: {verdict}")
    return result.returncode


def cmd_stamp(args):
    report = Path(args.report).read_text(encoding="utf-8")
    found = collections.defaultdict(set)
    for step, verdict in VERDICT.findall(report):
        found[step].add(verdict)
    missing = [step for step in STEPS if step not in found]
    if missing:
        die(f"report has no verdict for: {', '.join(missing)}")
    conflicting = [step for step, verdicts in found.items()
                   if len(verdicts) > 1]
    if conflicting:
        die(f"report gives conflicting verdicts for: "
            f"{', '.join(conflicting)}")
    verdicts = {step: found[step].pop() for step in STEPS}
    refused = [f"{step}: {verdict}" for step, verdict in verdicts.items()
               if verdict == "FAIL" or (verdict == "SKIP" and step != "bitbake")]
    if verdicts["bitbake"] == "SKIP" and not bitbake_skip_reason():
        refused.append("bitbake: SKIP in a clone where bitbake applies")
    if refused:
        die(f"not stamping, report has: {'; '.join(refused)}")

    head = git("rev-parse", "HEAD")
    results = run_static(head)
    if results.failures:
        die("not stamping, the static checks fail on HEAD")
    base, merge_base, commits, _ = review_range(head)
    directory = stamp_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / head).write_text(
        f"commit: {head}\nbase: {base} ({merge_base})\n"
        f"commits: {len(commits)}\n\n{report}")
    print(f"pre-mr: stamped {head[:12]} ({len(commits)} commit(s) over {base})")
    return 0


def passed(sha):
    """A commit may be pushed when it is stamped, or when everything it adds
    over the base, merges included, is already on a remote (a fork syncing
    upstream)."""
    if (stamp_dir() / sha).is_file():
        return True
    _, merge_base, commits, merges = review_range(sha)
    if not commits and not merges:
        return True
    return not git("rev-list", sha, "--not", merge_base, "--remotes")


def cmd_status(args):
    head = git("rev-parse", "HEAD")
    base, _, commits, merges = review_range(head)
    stamp = stamp_dir() / head
    print(f"HEAD {head[:12]}, {len(commits) + len(merges)} commit(s) over "
          f"{base}: ", end="")
    if not commits and not merges:
        print("nothing to review")
    elif stamp.is_file():
        print(f"passed\n\n{stamp.read_text()}")
    else:
        print("not passed; run the torizon-pre-push-triage skill")
    print(f"pre-push hook: {'installed' if hook_installed() else 'not installed'}")
    return 0 if passed(head) else 1


def hook_path():
    path = Path(git("rev-parse", "--git-path", "hooks/pre-push"))
    return path if path.is_absolute() else ROOT / path


def hook_installed():
    hook = hook_path()
    if not hook.is_file():  # follows a symlink, e.g. into a dotfiles repo
        return False
    return HOOK_MARK in hook.read_text(encoding="utf-8", errors="replace")


def cmd_install(args):
    # The copy lives in the git dir, shared by every worktree of the clone
    # and never part of a commit. Re-running install refreshes it.
    target = git_common_dir() / "pre-mr" / "pre-mr.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    if Path(__file__).resolve() != target.resolve():
        shutil.copyfile(__file__, target)
        target.chmod(0o755)
    hook = hook_path()
    if hook_installed():
        print(f"pre-mr: gate refreshed at {target}; hook already at {hook}")
        return 0
    hooks_path = run_git("config", "--get", "core.hooksPath").stdout.strip()
    line = ('python3 "$(git rev-parse --git-common-dir)/pre-mr/pre-mr.py" '
            'pre-push "$@" || exit 1')
    if hooks_path:
        die(f"core.hooksPath is set ({hooks_path}), so the hooks there serve "
            f"other repositories too; add this line to its pre-push hook:\n"
            f"  {line}")
    if hook.exists() or hook.is_symlink():
        die(f"{hook} already holds another hook; add this line to it:\n"
            f"  {line}")
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(INSTALLED_HOOK, encoding="utf-8")
    hook.chmod(0o755)
    print(f"pre-mr: installed the gate at {target} and the pre-push hook "
          f"at {hook}")
    return 0


def cmd_uninstall(args):
    hook = hook_path()
    if hook_installed():
        hook.unlink()
        print(f"pre-mr: removed {hook}")
    target = git_common_dir() / "pre-mr" / "pre-mr.py"
    if target.is_file():
        target.unlink()
        print(f"pre-mr: removed {target} (stamps kept)")
    return 0


def cmd_pre_push(args):
    blocked = []
    for line in sys.stdin:
        fields = line.split()
        if len(fields) != 4:
            continue
        local_ref, local_sha = fields[0], fields[1]
        if ZERO_SHA.match(local_sha):
            continue  # a deletion
        # A tag publishes the commit it points at.
        local_sha = git("rev-parse", f"{local_sha}^{{commit}}")
        if not passed(local_sha):
            blocked.append(f"{local_sha[:12]} ({local_ref})")
    if not blocked:
        return 0
    print("pre-push triage: these commits have not passed the triage:\n"
          + "".join(f"  {b}\n" for b in blocked) +
          "Ask Claude Code to run the torizon-pre-push-triage skill on that "
          "commit, then push again.\n"
          'Details: python3 "$(git rev-parse --git-common-dir)/pre-mr/'
          'pre-mr.py" status', file=sys.stderr)
    return 1


def main():
    global ROOT
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-C", dest="repo", default=".",
                        help="clone to work on (default: current directory)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("range", "static"):
        sub = commands.add_parser(name)
        sub.add_argument("rev", nargs="?", default="HEAD")
    commands.add_parser("bitbake")
    commands.add_parser("stamp").add_argument("report")
    commands.add_parser("status")
    commands.add_parser("install")
    commands.add_parser("uninstall")
    commands.add_parser("pre-push").add_argument("remote", nargs="*")
    args = parser.parse_args()
    top = subprocess.run(["git", "-C", args.repo, "rev-parse",
                          "--show-toplevel"], text=True, capture_output=True)
    if top.returncode != 0:
        die(f"{args.repo} is not inside a git work tree")
    ROOT = Path(top.stdout.strip()).resolve()
    handler = globals()["cmd_" + args.command.replace("-", "_")]
    sys.exit(handler(args))


if __name__ == "__main__":
    main()
