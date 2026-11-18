#!/usr/bin/env python3
"""Has any managed CR latched oasgen#818's never-clearing create-pending marker?

    KUBE_CONTEXT=<ctx> python3 -I scripts/create_pending_latch_check.py [--all-namespaces]
      exit 0  no CR is latched
      exit 1  at least one IS latched -- each is printed with its four timestamps
      exit 2  COULD NOT TELL -- no CRs examined, a stamp could not be ordered, or
              kubectl failed. NOT a pass.

THE DEFECT (present from oasgen 0.75.0 through 0.83.1, fixed by #825 in 0.84.0): a create
whose request answers 404 stamps `krateo.io/external-create-pending` and nothing ever
clears it. Its exposure is a FRESH install -- which is exactly what a bump-verification
cluster is -- so it bites the measurement rather than steady state.

THE TIMESTAMPS ARE PARSED, NOT STRING-COMPARED. See _ts: the two runtimes that write
these annotations use two different formats, and one of them writes the process's local
offset. A string compare is silently wrong in the direction that hides a latch.

THE PREDICATE, and why it is written this way. A marker is latched when

    pending  is strictly newer than  max(succeeded, failed, not-requested)

which is the same comparison `meta.ExternalCreateIncomplete` makes. Three things about it:

  * `not-requested` MUST be in the max. unstructured-runtime v1.17.1 added it as a third
    clearer, and a predicate written before it existed reports a latch on every no-create
    kind -- which is how this check produces a false positive on LoadBalancer and AiKey.
  * STRICTLY newer. Equal timestamps mean the pair was written in the same pass, which is
    the healthy shape measured at the 0.67.0 pin.
  * NO `Ready != True` FILTER. Relayed from the agentiko session and worth repeating: adding
    one HIDES real latches rather than narrowing the result. A latched CR can report Ready
    from an earlier successful pass, and that combination -- healthy status over a marker
    that will never clear -- is the case most worth finding.

WHY `latch_state` IS A PURE FUNCTION. Every run of this check against a real cluster has
reported zero, so its positive branch has never executed. A predicate whose positive branch
has never run is not an instrument; it is an untested claim about one. scripts/validate.py
asserts it against fixtures on every PR -- both outcomes, plus the control that tells this
predicate apart from the not-requested-blind one it replaced.
"""
import datetime
import json
import os
import subprocess
import sys

PENDING = "krateo.io/external-create-pending"
CLEARERS = ("krateo.io/external-create-succeeded",
            "krateo.io/external-create-failed",
            "krateo.io/external-create-not-requested")

LATCHED, CLEAN, NO_MARKER = "latched", "clean", "no-marker"


def _ts(value, what):
    """Parse an RFC3339 stamp into a comparable key, or raise ValueError.

    WHY NOT THE STRING COMPARE THIS USED TO DO. Lexicographic order on RFC3339 preserves
    chronological order only while every stamp carries the same offset AND the same number
    of fractional digits. Neither is guaranteed, and BOTH failures are silent in the
    direction that hides a latch. Measured, in the two runtimes that write these four
    annotations:

      unstructured-runtime (what THIS repo's controllers run on) -- v1.17.1, v1.27.1 and
        v1.28.1 all do `t.Format(time.RFC3339)` from `time.Now()`, at pkg/meta/meta.go:232+
        called from pkg/controller/worker.go. Second precision, no fraction, and NO .UTC():
        the offset is whatever zone the process is in. UTC only by the distroless default.
      provider-runtime -- `time.RFC3339Nano` from `time.Now()`, per that repo's own
        session. RFC3339Nano TRIMS TRAILING ZEROS, so `10:00:00Z` sorts AFTER
        `10:00:00.1Z` because 'Z' (0x5A) > '.' (0x2E) -- the later instant sorts first.

    So a tool that reads "the create annotations" meets both formats, and the offset hazard
    is live in the one that writes this repo's CRs. Parsing costs these lines; the caveat
    goes rather than getting documented.

    The fraction is split off BY HAND rather than left to the interpreter, for two reasons
    that are both about not trusting the instrument: how many fractional digits
    `fromisoformat` tolerates has changed across Python versions, and it TRUNCATES past the
    microsecond -- so `...123456789Z` and `...123456001Z` parse EQUAL and "strictly newer"
    reads a real latch as clean. The digits past the microsecond are carried separately and
    the returned key is a tuple, so ordering is exact at nanosecond precision.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("%s is empty, so it cannot be ordered" % what)
    # The only '.' in an RFC3339 stamp separates the fractional seconds; offsets use ':'.
    head, dot, rest = v.partition(".")
    sub = 0
    if dot:
        digits = ""
        while rest[:1].isdigit():
            digits, rest = digits + rest[0], rest[1:]
        if not digits:
            raise ValueError("%s has a '.' with no fractional digits: %r" % (what, value))
        v = head + "." + (digits + "000000")[:6] + rest
        sub = int((digits + "000000000")[6:9])      # nanoseconds past the microsecond
    if v[-1:] in ("Z", "z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(v)
    except ValueError as exc:
        raise ValueError("%s is not an RFC3339 timestamp: %r (%s)" % (what, value, exc))
    if dt.tzinfo is None:
        # A stamp with no offset cannot be ordered against one that has it. Both runtimes
        # always write an offset; something else wrote this, and assuming UTC is exactly
        # the guess this file exists to refuse.
        raise ValueError("%s carries no timezone offset, so it cannot be ordered: %r"
                         % (what, value))
    return (dt, sub)


def latch_state(ann):
    """LATCHED / CLEAN / NO_MARKER for one object's annotations. Raises on an unorderable stamp."""
    pend = ann.get(PENDING)
    if not pend:
        return NO_MARKER
    p = _ts(pend, PENDING)
    newest = None
    for c in CLEARERS:
        if ann.get(c):
            t = _ts(ann[c], c)
            if newest is None or t > newest:
                newest = t
    if newest is None:
        # pending with no clearer of any kind: the 404-create shape #818 leaves behind.
        return LATCHED
    return LATCHED if p > newest else CLEAN


def kubectl(args):
    ctx = os.environ.get("KUBE_CONTEXT")
    cmd = ["kubectl"] + (["--context", ctx] if ctx else []) + args
    out = subprocess.run(cmd, capture_output=True, text=True)
    return out.returncode, out.stdout, out.stderr


def main(argv):
    scope = ["--all-namespaces"] if "--all-namespaces" in argv else ["-A"]
    rc, out, err = kubectl(["api-resources", "--api-group=arubacloud.ogen.krateo.io",
                            "--no-headers", "-o", "name"])
    if rc != 0:
        print("COULD NOT TELL: listing the api-group failed: %s" % err.strip(), file=sys.stderr)
        return 2
    kinds = [l.split(".")[0] for l in out.split() if l and "configuration" not in l.lower()]
    if not kinds:
        print("COULD NOT TELL: no arubacloud.ogen.krateo.io resources are served, so no CR "
              "could be examined. A clean result over zero objects is not a clean result.",
              file=sys.stderr)
        return 2

    examined, latched, unparsable, unorderable = 0, [], [], []
    for kind in sorted(set(kinds)):
        rc, out, err = kubectl(["get", kind] + scope + ["-o", "json"])
        if rc != 0:
            continue                      # the kind may simply have no CRs
        try:
            items = (json.loads(out) or {}).get("items") or []
        except ValueError:
            unparsable.append(kind)
            continue
        for it in items:
            examined += 1
            md = (it.get("metadata") or {})
            ann = md.get("annotations") or {}
            try:
                state = latch_state(ann)
            except ValueError as exc:
                unorderable.append((kind, md.get("namespace"), md.get("name"), str(exc)))
                continue
            if state != LATCHED:
                continue
            latched.append((kind, md.get("namespace"), md.get("name"), ann.get(PENDING),
                            {c.rsplit("/", 1)[1]: ann.get(c, "-") for c in CLEARERS}))

    print("CRs examined   : %d" % examined)
    print("kinds served   : %d" % len(set(kinds)))
    if unparsable:
        print("CONTROL: %d kind(s) returned unparsable JSON: %s" % (len(unparsable), unparsable))
    if examined == 0:
        print("COULD NOT TELL: the kinds are served but no CR exists, so nothing was tested.",
              file=sys.stderr)
        return 2
    print("latched (#818) : %d" % len(latched))
    for kind, ns, name, pend, others in latched:
        print("  %s/%s/%s" % (kind, ns, name))
        print("    pending       %s" % pend)
        for k, v in others.items():
            print("    %-13s %s" % (k, v))

    if unorderable:
        print("CONTROL: %d CR(s) carry a stamp that could not be ordered:" % len(unorderable))
        for kind, ns, name, why in unorderable:
            print("  %s/%s/%s: %s" % (kind, ns, name, why))
        if not latched:
            print("COULD NOT TELL: a 'no latch' verdict that skipped those CRs is a false "
                  "zero.", file=sys.stderr)
            return 2
    return 1 if latched else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
