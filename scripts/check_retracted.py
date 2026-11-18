#!/usr/bin/env python3
"""Refuse a pin at a version the PRODUCER has retracted.

    python3 -I scripts/check_retracted.py <version> [--chart oasgen-provider]
      exit 0  not retracted
      exit 1  RETRACTED -- the reason and the replacement are printed
      exit 2  COULD NOT TELL -- fetch or parse failed, and that is NOT a pass

WHY THIS REPLACES A HAND-MAINTAINED SKIP LIST. scripts/pin_verify.sh carried its own skips
(0.66.0, 0.53.0, 0.54.0) and was therefore wrong the moment oasgen retracted something
nobody told us about. Walking from the then-current 0.67.0 pin upward it would have
sailed through
0.69.0, 0.70.0 and 0.77.0 -- all three published, all three installing cleanly, which by
the argument that found them is the DANGEROUS kind of bad version: one that does not
exist fails loudly at fetch, one that exists installs silently.

The producer owns the list, so reading it inherits future retractions for free. A
consumer-maintained copy drifts by construction.

FAIL CLOSED. If the list cannot be fetched or parsed, this exits 2 and the caller must
refuse the pin. "Could not read the retraction list" is not evidence that a version is
safe, and treating a network failure as a pass is how a gate becomes decoration.

CONSUMER-SPECIFIC SKIPS STAY IN pin_verify.sh. 0.53.0 and 0.54.0 are not in oasgen's list
-- they are this repo's own findings. The two kinds of skip are different: the producer's
list says "nobody should pin this", ours says "this repo cannot use this". Keeping them
separate is what stops our local reasons from looking like upstream policy, and what makes
it obvious which ones belong upstream if the reason generalises.
"""
import json
import sys
import urllib.request

URL = ("https://raw.githubusercontent.com/krateo-platformops/oasgen-provider/"
       "main/retracted-versions.yaml")
DEFAULT_CHART = "oasgen-provider"


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    version = argv[0]
    chart = DEFAULT_CHART
    if "--chart" in argv:
        i = argv.index("--chart")
        if i + 1 >= len(argv):
            print("--chart needs a value", file=sys.stderr)
            return 2
        chart = argv[i + 1]
    try:
        import yaml
    except ImportError:
        print("COULD NOT TELL: PyYAML is not importable", file=sys.stderr)
        return 2
    try:
        # No credentials, no headers identifying anyone: a public raw file.
        with urllib.request.urlopen(URL, timeout=20) as fh:
            if fh.status != 200:
                print("COULD NOT TELL: HTTP %s from the retraction list" % fh.status,
                      file=sys.stderr)
                return 2
            text = fh.read().decode("utf-8")
    except Exception as exc:                                  # noqa: BLE001
        print("COULD NOT TELL: fetching the retraction list failed: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return 2
    try:
        doc = yaml.safe_load(text)
    except Exception as exc:                                  # noqa: BLE001
        print("COULD NOT TELL: the retraction list did not parse: %s" % exc,
              file=sys.stderr)
        return 2
    entries = (doc or {}).get("retracted")
    if not isinstance(entries, list) or not entries:
        print("COULD NOT TELL: no `retracted` list in the document", file=sys.stderr)
        return 2

    # CONTROL on the parse itself: a list that yields no versions at all means the schema
    # moved under us, and every later `not in` would answer no for the wrong reason.
    all_versions = set()
    for e in entries:
        v = e.get("versions", e.get("version"))
        all_versions.update(v if isinstance(v, list) else [v] if v else [])
    if not all_versions:
        print("COULD NOT TELL: %d entries parsed but zero versions read -- the schema "
              "changed" % len(entries), file=sys.stderr)
        return 2

    for e in entries:
        v = e.get("versions", e.get("version"))
        versions = v if isinstance(v, list) else [v] if v else []
        if version not in versions:
            continue
        charts = e.get("charts") or []
        # An entry that does not name this chart retracts nothing FOR US. The list's own
        # header warns that a mis-scoped entry "retracts NOTHING, silently", so this is
        # reported rather than ignored.
        if charts and chart not in charts:
            print("NOTE: %s is listed, but scoped to %s and not to %s -- not refusing"
                  % (version, charts, chart))
            continue
        reason = (e.get("reason") or "").strip()
        print("RETRACTED: %s (chart %s)" % (version, chart))
        print("  use instead: %s" % (e.get("use") or "<the list names no replacement>"))
        print("  reason:")
        for line in reason.splitlines():
            print("    %s" % line)
        return 1

    print("%s is not retracted (list carries %d entries covering %s)"
          % (version, len(entries), ", ".join(sorted(all_versions))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
