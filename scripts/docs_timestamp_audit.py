#!/usr/bin/env python3
"""Audit docs frontmatter `timestamp` against each file's own last commit.

    python3 -I scripts/docs_timestamp_audit.py [--repo <path>] [--docs <subdir>]
      exit 0  every stamp is at or after its file's last commit
      exit 1  at least one stamp is OLDER than its own file's last commit
      exit 2  COULD NOT TELL -- no docs found, or git could not be read

WHY. The org `lint-docs` reusable warns `timestamp <date> predates the latest release
tag (review for drift)`. On this repo that fires on 25 of 25 docs, and on the installer
repo 8 of 8 -- i.e. on everything, in both. A warning that fires on 100% of files is not a
signal, it is something people learn to scroll past.

The comparison this script makes instead is against the file's OWN last commit, which is
the thing that can actually drift. The result is informative under either reading of a
field whose meaning is documented NOWHERE -- not in the workflow, not in either repo:

  * if `timestamp` means LAST MODIFIED, a stamp older than the file's last commit is
    simply wrong, and this prints exactly which files and by how much.
  * if it means LAST REVIEWED, the same rows say those files were edited after anyone
    last reviewed them, which is a different and worse problem.

WHAT THIS SCRIPT WILL NOT DO. It will not rewrite a stamp. Under the second reading that
would assert a review nobody performed, and a stamp that says "reviewed" because a script
said so is worse than a stale one: the next reader cannot tell which stamps were earned.
Fixing the data needs the semantic settled first, which is a question for whoever owns the
rule.

It also reports the lint rule's own comparison (stamp vs latest release tag) so the two
can be read side by side -- and because the release-tag figure is a function of release
cadence rather than of documentation, which is the structural complaint.
"""
import datetime
import os
import re
import subprocess
import sys


def git(repo, *args):
    out = subprocess.run(("git", "-C", repo) + args, capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else ""


def parse_iso(s):
    if not s:
        return None
    try:
        d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def frontmatter_timestamp(path):
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    m = re.match(r"^---\n(.*?)\n---", head, re.S)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith("timestamp:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None


def main(argv):
    repo, docs = ".", "docs"
    if "--repo" in argv:
        repo = argv[argv.index("--repo") + 1]
    if "--docs" in argv:
        docs = argv[argv.index("--docs") + 1]
    docs_dir = os.path.join(repo, docs)
    if not os.path.isdir(docs_dir):
        print("COULD NOT TELL: %s is not a directory" % docs_dir, file=sys.stderr)
        return 2
    files = sorted(f for f in os.listdir(docs_dir) if f.endswith(".md"))
    if not files:
        print("COULD NOT TELL: no .md files in %s" % docs_dir, file=sys.stderr)
        return 2
    # CONTROL: git must answer at all. Without this, an unreadable repo would report
    # "0 stale" -- a clean bill of health produced by a broken instrument.
    if not git(repo, "rev-parse", "--git-dir"):
        print("COULD NOT TELL: %s is not a git repository, so no file has a last-commit "
              "date to compare against" % repo, file=sys.stderr)
        return 2

    tag = git(repo, "for-each-ref", "--sort=-creatordate",
              "--format=%(refname:short)|%(creatordate:iso-strict)", "refs/tags")
    tag_name, tag_date = "", None
    if tag:
        first = tag.splitlines()[0]
        tag_name, _, raw = first.partition("|")
        tag_date = parse_iso(raw)

    rows, no_git_date, unparsable = [], [], []
    for name in files:
        rel = os.path.join(docs, name)
        stamp_raw = frontmatter_timestamp(os.path.join(repo, rel))
        stamp = parse_iso(stamp_raw) if stamp_raw else None
        if stamp_raw and stamp is None:
            unparsable.append(rel)
        commit_raw = git(repo, "log", "-1", "--format=%cI", "--", rel)
        commit = parse_iso(commit_raw)
        if commit is None:
            no_git_date.append(rel)
        rows.append((rel, stamp_raw or "<none>", stamp, commit_raw[:10] or "<none>", commit))

    # DATE granularity when the stamp carries no time, which every stamp in both repos
    # does (`2026-09-01T00:00:00Z`). A bare date normalises to midnight, so a datetime
    # comparison marks it older than ANY commit made later the same day -- and an author
    # who stamped the day they edited gets reported as stale. Measured: that false
    # positive is the entire difference between 25-of-25 and 23-of-25 on this repo, and it
    # surfaced only because an earlier ad-hoc pass compared dates and disagreed with this
    # script's first version.
    def older(stamp, stamp_raw, commit):
        if not stamp or not commit:
            return False
        bare = "T" not in (stamp_raw or "") or stamp_raw.endswith("T00:00:00Z")
        return stamp.date() < commit.date() if bare else stamp < commit

    stale = [r for r in rows if older(r[2], r[1], r[4])]
    pre_tag = [r for r in rows if r[2] and tag_date and r[2] < tag_date]

    print("repo: %s   docs: %d   latest tag: %s"
          % (os.path.abspath(repo), len(rows), tag_name or "<none>"))
    print()
    print("  %-46s %-12s %-12s" % ("DOC", "stamp", "last commit"))
    for rel, sraw, s, craw, c in rows:
        flag = "  <- stamp older than its own last edit" if older(s, sraw, c) else ""
        print("  %-46s %-12s %-12s%s" % (rel, sraw[:10], craw, flag))
    print()
    print("  stamp OLDER than the file's own last commit : %d of %d" % (len(stale), len(rows)))
    print("  stamp accurate (at or after last commit)    : %d" % (len(rows) - len(stale)))
    if tag_date:
        print("  the LINT RULE's comparison (stamp < latest tag): %d of %d"
              % (len(pre_tag), len(rows)))
        print("    note: that figure moves with RELEASE CADENCE, not with documentation --")
        print("    every new tag re-arms it on every doc not re-stamped after it.")
    else:
        print("  no tags in this repo, so the lint rule's comparison cannot fire here")
    if no_git_date:
        print("  CONTROL: %d file(s) have NO last-commit date (untracked or never "
              "committed) and are excluded from the counts above:" % len(no_git_date))
        for rel in no_git_date:
            print("    %s" % rel)
    if unparsable:
        print("  CONTROL: %d file(s) have a timestamp that is not ISO 8601:" % len(unparsable))
        for rel in unparsable:
            print("    %s" % rel)
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
