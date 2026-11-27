#!/usr/bin/env python3
"""Block pushes that would publish personal data.

Checks every commit about to be pushed (or, with --tree, every tracked file):

  * author / committer e-mail must be a GitHub noreply address
  * no "Co-Authored-By:" trailers (unless --allow-coauthors)
  * no home-directory paths, private keys, real-looking OTP secrets,
    database / key files, or e-mail addresses outside example.* domains
  * none of the words in your PRIVATE list (one per line, case-insensitive),
    kept outside the repository in  .git/privacy-patterns  so the list
    itself is never published

Install as a pre-push hook:   sh tools/install-hooks.sh
Run by hand:                  python3 tools/privacy_check.py --tree
"""
import os
import re
import subprocess
import sys

ZERO = "0" * 40
ALLOWED_EMAIL = re.compile(r"@(users\.noreply\.github\.com|noreply\.github\.com)$|^noreply@github\.com$", re.I)
DEMO_DOMAINS = re.compile(r"@(example\.(com|org|net)|[\w.-]+\.(test|example|invalid|localhost)"
                          r"|users\.noreply\.github\.com|noreply\.github\.com|noreply\.anthropic\.com)$", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
GENERIC = [
    (re.compile(r"/home/[a-z_][\w-]*/"), "a home-directory path (use ~/ instead)"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"secret=(?!JBSWY3DPEHPK3PXP\b)[A-Z2-7]{16,}", re.I), "a real-looking OTP secret"),
    (re.compile(r"\b(ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"), "a GitHub token"),
]
SECRET_FILES = re.compile(r"\.(kdbx|kdb|key|keyx|pem|p12)$|(^|/)(id_rsa|id_ed25519|\.env)$", re.I)
ALLOWED_FILES = re.compile(r"^tests/fixtures/")


def git(*args, check=True):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check).stdout


def private_patterns():
    path = os.path.join(git("rev-parse", "--git-dir").strip(), "privacy-patterns")
    try:
        with open(path, encoding="utf-8") as f:
            words = [w.strip() for w in f if w.strip() and not w.startswith("#")]
    except OSError:
        return []
    return [(re.compile(re.escape(w), re.I), "a word from your private list") for w in words]


def scan_text(text, where, patterns, problems):
    for n, line in enumerate(text.splitlines(), 1):
        for rx, what in patterns:
            if rx.search(line):
                problems.append("%s:%d: %s" % (where, n, what))
        for m in EMAIL.finditer(line):
            if not DEMO_DOMAINS.search(m.group(0)):
                problems.append("%s:%d: a real e-mail address (use alex@example.com)" % (where, n))


def check_commit(sha, patterns, problems, allow_coauthors):
    short = sha[:8]
    ae, ce = git("show", "-s", "--format=%ae%n%ce", sha).split()
    for kind, email in (("author", ae), ("committer", ce)):
        if not ALLOWED_EMAIL.search(email):
            problems.append("commit %s: %s e-mail is not a GitHub noreply address" % (short, kind))
    msg = git("show", "-s", "--format=%B", sha)
    if not allow_coauthors and re.search(r"^co-authored-by:", msg, re.I | re.M):
        problems.append("commit %s: has a Co-Authored-By trailer" % short)
    scan_text(msg, "commit %s message" % short, patterns, problems)
    for status_line in git("show", "--format=", "--name-status", "--no-renames", sha).splitlines():
        status, _, name = status_line.partition("\t")
        if status != "D" and SECRET_FILES.search(name) and not ALLOWED_FILES.search(name):
            problems.append("commit %s: adds %s (database / key file)" % (short, name))
    added = "\n".join(l[1:] for l in git("show", "--format=", "-U0", "--no-color", sha).splitlines()
                      if l.startswith("+") and not l.startswith("+++"))
    scan_text(added, "commit %s" % short, patterns, problems)


def commits_to_push(stdin_lines):
    shas = []
    for line in stdin_lines:
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO:
            continue
        local, remote = parts[1], parts[3]
        rng = [local, "--not", "--remotes"] if remote == ZERO else ["%s..%s" % (remote, local)]
        out = git("rev-list", *rng, check=False)
        shas += [s for s in out.split() if s not in shas]
    return shas


def main():
    args = sys.argv[1:]
    allow = "--allow-coauthors" in args
    patterns = GENERIC + private_patterns()
    problems = []
    if "--tree" in args:
        for name in git("ls-files").splitlines():
            if SECRET_FILES.search(name) and not ALLOWED_FILES.search(name):
                problems.append("%s: database / key file is tracked" % name)
                continue
            try:
                with open(name, encoding="utf-8") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError):
                continue  # images and other binary files
            if name == "tools/privacy_check.py":
                continue  # the patterns themselves
            scan_text(text, name, patterns, problems)
    else:
        for sha in commits_to_push(sys.stdin.read().splitlines()):
            check_commit(sha, patterns, problems, allow)
    if problems:
        print("Privacy check FAILED: this would publish personal data.\n", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        print("\nFix the commit(s) (git commit --amend / git rebase) and push again.", file=sys.stderr)
        return 1
    print("Privacy check passed (%d private words checked)." % (len(patterns) - len(GENERIC)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
