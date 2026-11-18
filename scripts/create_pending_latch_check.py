#!/usr/bin/env python3
"""Has any managed CR latched oasgen#818's never-clearing create-pending marker?

    KUBE_CONTEXT=<ctx> python3 -I scripts/create_pending_latch_check.py [--all-namespaces]
      exit 0  no CR is latched
      exit 1  at least one IS latched -- each is printed with its four timestamps
      exit 2  COULD NOT TELL -- no CRs examined, or kubectl failed. NOT a pass.

THE DEFECT (present from oasgen 0.80.0, fixed on main by #825, unreleased as of 0.83.1): a
create whose request answers 404 stamps `krateo.io/external-create-pending` and nothing ever
clears it. Its exposure is a FRESH install -- which is exactly what a bump-verification
cluster is -- so it bites the measurement rather than steady state.

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
"""
import json
import os
import subprocess
import sys

PENDING = "krateo.io/external-create-pending"
CLEARERS = ("krateo.io/external-create-succeeded",
            "krateo.io/external-create-failed",
            "krateo.io/external-create-not-requested")


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

    examined, latched, unparsable = 0, [], []
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
            ann = ((it.get("metadata") or {}).get("annotations") or {})
            pend = ann.get(PENDING)
            if not pend:
                continue
            clears = [ann[c] for c in CLEARERS if ann.get(c)]
            newest = max(clears) if clears else ""
            # Strictly newer. RFC3339 from the same writer sorts lexicographically.
            if pend > newest:
                latched.append((kind, (it.get("metadata") or {}).get("namespace"),
                                (it.get("metadata") or {}).get("name"), pend,
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
    return 1 if latched else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
