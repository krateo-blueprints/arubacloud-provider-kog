#!/usr/bin/env python3
"""Snapshot the in-place upgrade subject, and compare two snapshots.

    KUBECONFIG=... python3 -I scripts/upgrade_snapshot.py --take  <out.json>
                   python3 -I scripts/upgrade_snapshot.py --diff  <before.json> <after.json> [--to X.Y.Z]

      exit 0  the upgrade is survivable by every assertion below
      exit 1  at least one assertion FAILED -- each is printed
      exit 2  THE RESULT MEANS NOTHING: a snapshot was empty, the two snapshots are not
              of the same objects, or kubectl failed

WHY THIS EXISTS. docs/oasgen-provider-evolution.md retracted "upgrade, then diff the CRDs"
as an instrument, and the retraction was right: a CRD diff cannot distinguish "nothing
changed" from "nothing was re-rendered", because both look identical. The field that
resolves it is `status.generatorBuild` -- the generator's own identity, which
oasgen-provider's restdefinition.go compares as the FIFTH drift trigger:

    if e.genCfg.generatorChanged(cr.Status.GeneratorBuild) { ... regenerating the CRD }

    it MOVES ................. a re-render demonstrably happened
    it does NOT move while
    the provider version did .. the CRD was left on the old generator while the
                                RestDefinition kept reporting Ready -- oasgen-provider#453

So this records generatorBuild and asserts on it, and treats the CRD schema hash as
corroborating detail rather than as the measurement.

WHAT IS RECORDED, AND WHY EACH ONE

  restdefinitions  name, Ready + its reason, generatorBuild, generation/observedGeneration
  crds             resourceVersion, storedVersions, and a sha256 over THE SCHEMA ONLY.
                   Not the whole object: its metadata moves for reasons that are not a
                   re-render, so a whole-object hash makes every run look like a change.
  crs              uid (so a RECREATED CR cannot pass as a survivor), the spec key sets,
                   a spec hash, conditions, and the krateo.io/* annotations.

                   KEY SETS AND NOT ONLY A HASH, because a hash says something changed
                   while the key sets say WHAT APPEARED. A stored object re-read under a
                   new schema is where a structural change surfaces -- and it surfaces
                   with no etcd write and no resourceVersion bump, so the appearing field
                   is the only visible trace. `default: {}` on a new property is the
                   shape that does this.

THE CONTROLS, because most of this repo's instrument failures were empty measurements:
  * a snapshot with zero RestDefinitions, zero CRDs or zero CRs REFUSES to be written.
    An empty snapshot compares equal to another empty one and reads as "nothing changed"
    when it means "nothing was looked at".
  * a diff whose two sides do not name the same CR uids is VOID, not a pass: if the CRs
    were recreated between the snapshots, the axis measured a fresh install.
  * --to X.Y.Z asserts the generatorBuild actually carries the version the upgrade was
    supposed to install, so "it moved" cannot pass on a move to the wrong build.
"""
import hashlib
import json
import subprocess
import sys

GROUP = "arubacloud.ogen.krateo.io"
KINDS = ("vpcs", "subnets", "securitygroups", "securityrules")


def kc(args):
    r = subprocess.run(["kubectl"] + args, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def take(path):
    out = {"restdefinitions": [], "crds": {}, "crs": [], "control": {}}

    rc, o, e = kc(["get", "restdefinitions.ogen.krateo.io", "-A", "-o", "json"])
    if rc != 0:
        print("snapshot: listing RestDefinitions failed: %s" % e.strip(), file=sys.stderr)
        return 2
    for it in (json.loads(o).get("items") or []):
        st = it.get("status") or {}
        ready = [c for c in (st.get("conditions") or []) if c.get("type") == "Ready"]
        out["restdefinitions"].append({
            "name": it["metadata"]["name"],
            "ready": ready[0]["status"] if ready else None,
            "readyReason": ready[0].get("reason") if ready else None,
            "readyMessage": (ready[0].get("message") or "")[:200] if ready else None,
            "generatorBuild": st.get("generatorBuild"),
            "observedGeneration": st.get("observedGeneration"),
            "generation": it["metadata"].get("generation"),
        })

    rc, o, e = kc(["get", "crd", "-o", "json"])
    if rc != 0:
        print("snapshot: listing CRDs failed: %s" % e.strip(), file=sys.stderr)
        return 2
    for it in (json.loads(o).get("items") or []):
        n = it["metadata"]["name"]
        if not n.endswith(GROUP):
            continue
        vers = (it.get("spec") or {}).get("versions") or []
        schema = json.dumps([v.get("schema") for v in vers], sort_keys=True)
        out["crds"][n] = {
            "resourceVersion": it["metadata"]["resourceVersion"],
            "schemaSHA": hashlib.sha256(schema.encode()).hexdigest()[:16],
            "schemaBytes": len(schema),
            "storedVersions": (it.get("status") or {}).get("storedVersions"),
            "versions": [v.get("name") for v in vers],
        }

    for kind in KINDS:
        rc, o, e = kc(["get", kind, "-A", "-o", "json"])
        if rc != 0:
            continue                      # the kind may not be served in this scope
        for it in (json.loads(o).get("items") or []):
            md, st = it["metadata"], (it.get("status") or {})
            spec = it.get("spec") or {}
            out["crs"].append({
                "kind": it["kind"], "name": md["name"], "namespace": md.get("namespace"),
                "uid": md.get("uid"),
                "resourceVersion": md["resourceVersion"],
                "generation": md.get("generation"),
                "conditions": {c["type"]: c["status"]
                               for c in (st.get("conditions") or [])},
                "annotations": {k: v for k, v in (md.get("annotations") or {}).items()
                                if k.startswith("krateo.io/")},
                "specSHA": hashlib.sha256(
                    json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16],
                "specKeys": sorted(spec.keys()),
                "specPropKeys": sorted((spec.get("properties") or {}).keys()),
                "statusKeys": sorted(st.keys()),
            })

    out["control"] = {"restdefinitions": len(out["restdefinitions"]),
                      "crds": len(out["crds"]), "crs": len(out["crs"])}
    if not all(out["control"].values()):
        print("snapshot: REFUSING to write an empty snapshot: %s\n"
              "    An empty snapshot compares equal to another empty one and reads as\n"
              "    'nothing changed' when it means 'nothing was looked at'."
              % out["control"], file=sys.stderr)
        return 2
    json.dump(out, open(path, "w"), indent=2, sort_keys=True)
    print("snapshot written: %s  -> %s" % (out["control"], path))
    return 0


# Defaults for every field the diff reads. A snapshot taken by an EARLIER version of this
# script lacks whatever was added since, and a KeyError on one of those exits 1 -- which is
# also the "not clean" code, so an instrument failure reads as a finding about the world.
# That is the mistake this repo keeps paying for, so the diff normalises both sides first
# and main() turns any unexpected exception into VOID rather than into a verdict.
RD_DEFAULTS = {"name": None, "ready": None, "readyReason": None, "readyMessage": None,
               "generatorBuild": None, "observedGeneration": None, "generation": None}
CRD_DEFAULTS = {"resourceVersion": None, "schemaSHA": None, "schemaBytes": 0,
                "storedVersions": [], "versions": []}
CR_DEFAULTS = {"kind": None, "name": None, "namespace": None, "uid": None,
               "resourceVersion": None, "generation": None, "conditions": {},
               "annotations": {}, "specSHA": None, "specKeys": [], "specPropKeys": [],
               "statusKeys": []}


def _norm(snap, label):
    """Fill in fields a snapshot from another version of this script may not carry."""
    missing = set()
    snap.setdefault("control", {})
    snap["restdefinitions"] = [dict(RD_DEFAULTS, **r) for r in snap.get("restdefinitions") or []]
    snap["crs"] = [dict(CR_DEFAULTS, **c) for c in snap.get("crs") or []]
    crds = {}
    for n, c in (snap.get("crds") or {}).items():
        crds[n] = dict(CRD_DEFAULTS, **c)
    snap["crds"] = crds
    for recs, defaults in ((snap["restdefinitions"], RD_DEFAULTS),
                           (snap["crs"], CR_DEFAULTS)):
        for r in recs:
            missing |= {k for k in defaults if k not in r}
    if missing:
        print("  . %s was taken by another version of this script; filled defaults for %s"
              % (label, sorted(missing)))
    return snap


def diff(before_path, after_path, to_version=None):
    a = _norm(json.load(open(before_path)), before_path)
    b = _norm(json.load(open(after_path)), after_path)

    void, fail, note = [], [], []

    if not all(a.get("control", {}).values()) or not all(b.get("control", {}).values()):
        void.append("one side is an empty snapshot: before=%s after=%s"
                    % (a.get("control"), b.get("control")))

    auid = {c["uid"]: c for c in a["crs"] if c.get("uid")}
    buid = {c["uid"]: c for c in b["crs"] if c.get("uid")}
    if not auid or not buid:
        void.append("a side records no CR uids, so survival cannot be established")
    gone = sorted(set(auid) - set(buid))
    fresh = sorted(set(buid) - set(auid))
    if gone:
        fail.append("%d CR(s) present before are GONE after: %s"
                    % (len(gone), [auid[u]["kind"] + "/" + auid[u]["name"] for u in gone]))
    if fresh:
        void.append("%d CR(s) after the upgrade have uids that were not there before: %s"
                    " -- if the CRs were recreated, this run measured a fresh install"
                    % (len(fresh), [buid[u]["kind"] + "/" + buid[u]["name"] for u in fresh]))

    # --- the measurement: did the generator identity move, and is everything Ready
    abuild = {r["name"]: r for r in a["restdefinitions"]}
    bbuild = {r["name"]: r for r in b["restdefinitions"]}
    if not abuild:
        void.append("the before snapshot records no RestDefinitions")
    for n in sorted(abuild):
        if n not in bbuild:
            fail.append("RestDefinition %s is GONE after the upgrade" % n)
            continue
        ra, rb = abuild[n], bbuild[n]
        if rb["ready"] != "True":
            fail.append("RestDefinition %s is Ready=%s after the upgrade (reason=%s) %s"
                        % (n, rb["ready"], rb["readyReason"], rb["readyMessage"] or ""))
        if ra["generatorBuild"] == rb["generatorBuild"]:
            fail.append(
                "RestDefinition %s kept generatorBuild %s across the upgrade. If the "
                "provider version DID change, the CRD was left on the old generator "
                "while the RestDefinition reported Ready -- oasgen-provider#453."
                % (n, ra["generatorBuild"]))
        elif to_version and not str(rb["generatorBuild"] or "").startswith(to_version):
            fail.append("RestDefinition %s moved to generatorBuild %s, which does not "
                        "carry the expected version %s"
                        % (n, rb["generatorBuild"], to_version))
        else:
            note.append("%-32s generatorBuild %s -> %s" % (n, ra["generatorBuild"],
                                                           rb["generatorBuild"]))

    # --- corroboration, not the measurement
    for n in sorted(set(a["crds"]) | set(b["crds"])):
        ca, cb = a["crds"].get(n), b["crds"].get(n)
        if ca and not cb:
            fail.append("CRD %s is GONE after the upgrade" % n)
        elif cb and not ca:
            note.append("CRD %s is NEW after the upgrade" % n)
        elif ca["storedVersions"] != cb["storedVersions"]:
            fail.append("CRD %s storedVersions changed %s -> %s: stored objects now need "
                        "a migration" % (n, ca["storedVersions"], cb["storedVersions"]))
        elif ca["schemaSHA"] != cb["schemaSHA"]:
            note.append("CRD %-44s schema CHANGED  %s -> %s  (%d -> %d bytes)"
                        % (n, ca["schemaSHA"], cb["schemaSHA"],
                           ca["schemaBytes"], cb["schemaBytes"]))
        else:
            note.append("CRD %-44s schema identical (%s)" % (n, ca["schemaSHA"]))

    # --- the stored-object question: what does a CR look like when re-read
    for u in sorted(set(auid) & set(buid)):
        ca, cb = auid[u], buid[u]
        label = "%s/%s" % (ca["kind"], ca["name"])
        appeared = sorted(set(cb["specPropKeys"]) - set(ca["specPropKeys"]))
        vanished = sorted(set(ca["specPropKeys"]) - set(cb["specPropKeys"]))
        if appeared:
            fail.append("%s came back with spec.properties it did not have: %s. A field "
                        "appearing on a stored object re-read under a new schema is a "
                        "structural change, and it arrives with no etcd write and no "
                        "resourceVersion bump." % (label, appeared))
        if vanished:
            fail.append("%s lost spec.properties: %s" % (label, vanished))
        if sorted(cb["specKeys"]) != sorted(ca["specKeys"]):
            fail.append("%s top-level spec keys changed %s -> %s"
                        % (label, ca["specKeys"], cb["specKeys"]))
        for cond in ("Synced", "Ready"):
            was, now = ca["conditions"].get(cond), cb["conditions"].get(cond)
            if was == "True" and now != "True":
                fail.append("%s was %s=True before and is %s=%s after"
                            % (label, cond, cond, now))
        newann = sorted(set(cb["annotations"]) - set(ca["annotations"]))
        if newann:
            note.append("%-24s new krateo.io annotations: %s" % (label, newann))
        if ca["specSHA"] != cb["specSHA"]:
            note.append("%-24s spec hash changed %s -> %s (keys unchanged)"
                        % (label, ca["specSHA"], cb["specSHA"]))

    print("=== upgrade axis: %s -> %s" % (before_path, after_path))
    print("    before: %s" % a.get("control"))
    print("    after : %s" % b.get("control"))
    print()
    for n in note:
        print("  . %s" % n)
    print()
    if fail:
        print("FAILED assertions (%d):" % len(fail))
        for f in fail:
            print("  ! %s" % f)
    if void:
        print("CONTROL FAILURES (%d) -- the result means nothing:" % len(void))
        for v in void:
            print("  VOID %s" % v)
        print("\nVERDICT: VOID")
        return 2
    if fail:
        print("\nVERDICT: the upgrade is NOT clean by these assertions")
        return 1
    print("VERDICT: every assertion held -- generatorBuild moved, every RestDefinition "
          "re-reached Ready, every CR survived with its uid, spec keys and conditions")
    return 0


def main(argv):
    if "--take" in argv:
        i = argv.index("--take")
        if i + 1 >= len(argv):
            return int(bool(print(__doc__.strip(), file=sys.stderr))) or 2
        return take(argv[i + 1])
    if "--diff" in argv:
        i = argv.index("--diff")
        rest = [a for a in argv[i + 1:] if not a.startswith("--")]
        if len(rest) < 2:
            print(__doc__.strip(), file=sys.stderr)
            return 2
        to = None
        if "--to" in argv:
            j = argv.index("--to")
            to = argv[j + 1] if j + 1 < len(argv) else None
        return diff(rest[0], rest[1], to)
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:
        # Exit 1 means "the upgrade is not clean", which is a statement about the
        # cluster. A crash is a statement about this script, and must not borrow that
        # code: it exits 2, VOID, like every other case where the result means nothing.
        import traceback
        traceback.print_exc()
        print("\nVERDICT: VOID -- %s crashed (%s), so it measured nothing"
              % (sys.argv[0], type(exc).__name__))
        sys.exit(2)
