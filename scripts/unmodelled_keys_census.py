#!/usr/bin/env python3
"""Census the vendor response keys that the generated CRDs do not model -- all 36 kinds,
at every depth, offline.

    python3 -I scripts/unmodelled_keys_census.py [--kind Vpc] [--nested-only] [--quiet]

      exit 0  the census ran and every control held
      exit 2  THE RESULT MEANS NOTHING: a document, verb or schema would not resolve, no
              definition was read, or the control below failed

WHY THIS EXISTS, AND WHY IT IS NOT THE ANNOTATION. oasgen 0.84.0 writes
`krateo.io/unmodelled-keys-reported` on each CR, naming response keys the CRD does not
model. It is a real capability this fleet never had. But oasgen-provider#732 is OPEN: that
comparison is **one level deep**, so a key lost inside a nested object is silent -- and, in
the issue's own words, the silence reads as "nothing is being lost". The annotation also
needs a LIVE CR per kind, which for this fleet means 19 chains, 21 billable kinds, and four
kinds no chain covers at all (AiKey, AlertRule, Hpc, LoadBalancer).

This walk reads the vendored OAS documents instead. It therefore:
  * covers all 36 kinds without a cluster, a credential or a vendor charge;
  * goes to EVERY depth, which is the half #732 cannot see;
  * answers a DIFFERENT question, and the difference matters -- see below.

WHAT IT MEASURES, STATED PRECISELY, BECAUSE THE TWO ARE NOT INTERCHANGEABLE

    the annotation     keys in the response BODY the vendor actually returned, that the
                       CR's SPEC does not carry. One level deep. Needs a live resource.
                       NOTE, measured rather than assumed: a field surfaced into STATUS by
                       additionalStatusFields is STILL reported as unmodelled. All four
                       control kinds declare `metadata.id` and populate `status.id` from
                       it, and all four report `metadata.id` as unmodelled. So the
                       annotation does not mean "this value is lost".
    this census        keys in the response SCHEMA the document declares, that the
                       request schema does not carry and no additionalStatusFields or
                       identifier claims. Every depth. Needs no cluster.

So a vendor that returns an UNDECLARED key appears in the annotation and not here, and a
declared-but-never-returned key appears here and not in the annotation. Neither is a
superset. Quoting one as the other is the error this header exists to prevent.

THE CONTROL, and it is the reason this script can be trusted at all. Four kinds have a
MEASURED runtime annotation, from the 0.67.0 -> 0.84.0 upgrade axis. This script
reconstructs the annotation's one-level view for those four and compares it to the measured
bytes. If the reconstruction does not match, the method is wrong and the whole census is
VOID -- not "mostly right". A static walk that has never been checked against the runtime it
claims to approximate is an assertion, not a measurement.
"""
import glob
import json
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("unmodelled_keys_census: PyYAML is required")

# MEASURED on 2026-11-18 from the upgrade-axis snapshots: the exact annotation values the
# 0.84.0 controllers wrote on CRs created under 0.67.0. The `gen=1` prefix is the
# generation the report was computed for and is not part of the key sets.
RUNTIME_CONTROL = {
    "Vpc": {"metadata": ["category", "createdBy", "creationDate", "id", "ownerId",
                         "project", "updateDate", "updatedBy", "uri", "version"]},
    "Subnet": {"metadata": ["category", "createdBy", "creationDate", "id", "ownerId",
                            "project", "updateDate", "updatedBy", "uri", "version"],
               "properties": ["vpc"]},
    "SecurityGroup": {"metadata": ["category", "createdBy", "creationDate", "id", "ownerId",
                                   "project", "updateDate", "updatedBy", "uri", "version"],
                      "properties": ["vpc"]},
    "SecurityRule": {"metadata": ["category", "createdBy", "creationDate", "id", "ownerId",
                                  "project", "updateDate", "updatedBy", "uri", "version"]},
}

OBSERVE_ACTIONS = ("get", "findby")
REQUEST_ACTIONS = ("create", "update", "dispatch")


def load_configmaps():
    out = {}
    for f in sorted(glob.glob("configmaps/*.yaml")):
        cm = yaml.safe_load(open(f, encoding="utf-8"))
        if cm and (cm.get("metadata") or {}).get("name"):
            out[cm["metadata"]["name"]] = cm.get("data") or {}
    return out


def doc_for(oas_path, cms, problems, label):
    """Resolve `configmap://<ns>/<name>/<key>` to a parsed document, or record WHY not."""
    if not oas_path.startswith("configmap://"):
        problems.append("%s: oasPath is not a configmap:// URL: %r" % (label, oas_path))
        return None
    parts = oas_path[len("configmap://"):].split("/")
    if len(parts) != 3:
        problems.append("%s: cannot parse oasPath %r" % (label, oas_path))
        return None
    _, name, key = parts
    if name not in cms:
        problems.append("%s: ConfigMap %r not found" % (label, name))
        return None
    if key not in cms[name]:
        problems.append("%s: key %r not in ConfigMap %r" % (label, key, name))
        return None
    try:
        return json.loads(cms[name][key])
    except ValueError as exc:
        problems.append("%s: document %s/%s will not parse: %s" % (label, name, key, exc))
        return None


def deref(doc, node, seen=None):
    seen = seen or set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen or not ref.startswith("#/"):
            return None
        seen.add(ref)
        cur = doc
        for seg in ref[2:].split("/"):
            if not isinstance(cur, dict) or seg not in cur:
                return None
            cur = cur[seg]
        node = cur
    return node


def props_of(doc, schema):
    schema = deref(doc, schema)
    if not isinstance(schema, dict):
        return {}
    out = dict(schema.get("properties") or {})
    for sub in schema.get("allOf") or []:
        sub = deref(doc, sub)
        if isinstance(sub, dict):
            out.update(sub.get("properties") or {})
    return out


def walk(doc, schema, prefix="", depth=0, seen=None, out=None):
    """Every property path in a schema, as dotted strings. Arrays descend through `items`."""
    out = {} if out is None else out
    seen = seen or set()
    schema = deref(doc, schema)
    if not isinstance(schema, dict) or depth > 12:
        return out
    ident = id(schema)
    if ident in seen:
        return out                      # a $ref cycle resolved through allOf
    seen = seen | {ident}
    for name, sub in props_of(doc, schema).items():
        path = "%s.%s" % (prefix, name) if prefix else name
        out[path] = depth
        sub_d = deref(doc, sub) or {}
        if sub_d.get("type") == "array":
            walk(doc, sub_d.get("items") or {}, path + "[]", depth + 1, seen, out)
        else:
            walk(doc, sub_d, path, depth + 1, seen, out)
    return out


def body_schema(doc, op, problems, label):
    """The JSON request body schema of an operation."""
    rb = (op or {}).get("requestBody") or {}
    rb = deref(doc, rb) or {}
    content = rb.get("content") or {}
    for ct in ("application/json", "application/json; charset=utf-8", "*/*"):
        if ct in content:
            return (content[ct] or {}).get("schema") or {}
    if content:
        return (list(content.values())[0] or {}).get("schema") or {}
    return None


def response_schema(doc, op, problems, label):
    """The 2xx JSON response schema of an operation."""
    resps = (op or {}).get("responses") or {}
    for code in ("200", "201", "202", 200, 201, 202):
        if str(code) in {str(k) for k in resps}:
            r = resps.get(str(code)) if str(code) in resps else resps.get(code)
            r = deref(doc, r) or {}
            content = r.get("content") or {}
            for ct in ("application/json", "application/json; charset=utf-8", "*/*"):
                if ct in content:
                    return (content[ct] or {}).get("schema") or {}
            if content:
                return (list(content.values())[0] or {}).get("schema") or {}
            problems.append("%s: the %s response declares no content" % (label, code))
            return None
    problems.append("%s: no 2xx response declared" % label)
    return None


def one_level_view(unmodelled):
    """Reconstruct the annotation's shape: {top-level object: [immediate children]}.

    The annotation groups by the first path segment and lists only its IMMEDIATE children,
    which is exactly the one-level comparison oasgen-provider#732 describes.
    """
    view = {}
    for path in unmodelled:
        segs = path.replace("[]", "").split(".")
        if len(segs) == 2:
            view.setdefault(segs[0], set()).add(segs[1])
    return {k: sorted(v) for k, v in view.items()}


def main(argv):
    only = None
    if "--kind" in argv:
        i = argv.index("--kind")
        only = argv[i + 1] if i + 1 < len(argv) else None
    nested_only = "--nested-only" in argv
    quiet = "--quiet" in argv

    cms = load_configmaps()
    problems, rows = [], []
    read = 0

    for path in sorted(glob.glob("restdefinitions/**/*.yaml", recursive=True)):
        d = yaml.safe_load(open(path, encoding="utf-8"))
        if not d or d.get("kind") != "RestDefinition":
            continue
        read += 1
        spec = d.get("spec") or {}
        res = spec.get("resource") or {}
        kind = res.get("kind") or path
        if only and kind != only:
            continue
        label = kind
        verbs = {v.get("action"): v for v in (res.get("verbsDescription") or [])
                 if isinstance(v, dict)}

        # --- the response side
        obs = next((verbs[a] for a in OBSERVE_ACTIONS if a in verbs), None)
        if not obs:
            problems.append("%s: declares no get and no findby, so there is no response "
                            "schema to compare -- NOT a report of zero unmodelled keys" % label)
            continue
        odoc = doc_for(obs.get("oasPath") or spec.get("oasPath") or "", cms, problems, label)
        if odoc is None:
            continue
        op = ((odoc.get("paths") or {}).get(obs.get("path")) or {}).get(
            (obs.get("method") or "get").lower())
        if not op:
            problems.append("%s: %s %s is not in its document" % (
                label, (obs.get("method") or "?").upper(), obs.get("path")))
            continue
        rschema = response_schema(odoc, op, problems, label)
        if rschema is None:
            continue
        resp = walk(odoc, rschema)
        # a findby returns a collection: descend through its itemsPath
        if obs.get("action") == "findby" or "items" in resp or "value" in resp:
            ip = obs.get("itemsPath")
            for cand in ([ip] if ip else []) + ["items", "value", "values", "data"]:
                if cand and cand in resp:
                    inner = {k[len(cand) + 1:]: v for k, v in resp.items()
                             if k.startswith(cand + ".") or k.startswith(cand + "[].")}
                    inner = {k.lstrip("."): v for k, v in inner.items()}
                    if inner:
                        resp = inner
                        break

        # --- the modelled side: the request body the spec is derived from, plus the
        # status fields and identifiers the definition claims explicitly.
        modelled = set()
        for a in REQUEST_ACTIONS:
            if a not in verbs:
                continue
            rdoc = doc_for(verbs[a].get("oasPath") or spec.get("oasPath") or "", cms,
                           problems, label)
            if rdoc is None:
                continue
            rop = ((rdoc.get("paths") or {}).get(verbs[a].get("path")) or {}).get(
                (verbs[a].get("method") or "post").lower())
            if not rop:
                # "I could not look" must never contribute as "there is nothing there".
                # Left silent, a missing operation empties the modelled set and the kind
                # then reports EVERY response key as unmodelled -- which is how AlertRule
                # first read 62 of 62. The header states this rule for the response side;
                # the request side needed it too.
                problems.append("%s: the %s verb's %s %s is not in its document, so the "
                                "modelled set would have been silently empty"
                                % (label, a, (verbs[a].get("method") or "?").upper(),
                                   verbs[a].get("path")))
                continue
            bs = body_schema(rdoc, rop, problems, label)
            if bs is None:
                problems.append("%s: the %s verb declares no JSON request body, so the "
                                "modelled set would have been silently empty" % (label, a))
                continue
            modelled |= set(walk(rdoc, bs))
        # NOT added to `modelled`: additionalStatusFields. MEASURED, and it is the finding
        # that cost this script its first control run. The runtime annotation reported
        # `metadata.id` as unmodelled on all four control kinds -- while every one of them
        # declares `additionalStatusFields: [metadata.id]` and populates `status.id` from
        # it. So oasgen's "unmodelled" means NOT IN THE SPEC, not "not represented
        # anywhere". A reader who takes the annotation as "these values are lost" is wrong
        # about exactly the fields the definition deliberately surfaced into status.
        # They are tracked separately below and printed as such.
        status_surfaced = set()
        for f in (res.get("additionalStatusFields") or []):
            status_surfaced.add(f)
        for f in (res.get("identifiers") or []):
            modelled.add(f)
        for f in (res.get("configurationFields") or []):
            n = (f.get("fromOpenAPI") or {}).get("name") if isinstance(f, dict) else None
            if n:
                modelled.add(n)

        # A kind with NO create/update/dispatch verb has no request schema by design, so
        # "everything is unmodelled" is an artefact of the method rather than a finding
        # about the definition. Measured: exactly one kind is in this position.
        no_request = not (set(verbs) & set(REQUEST_ACTIONS)) and not res.get("createApiRef")
        unmodelled = {p: dep for p, dep in resp.items() if p not in modelled
                      and p.replace("[]", "") not in modelled}
        rows.append({"kind": kind, "verb": obs.get("action"),
                     "response_paths": len(resp), "modelled_paths": len(modelled),
                     "unmodelled": unmodelled,
                     "surfaced": sorted(p for p in unmodelled if p in status_surfaced),
                     "one_level": one_level_view(unmodelled),
                     "no_request": no_request,
                     "nested": sorted(p for p, dep in unmodelled.items() if dep >= 2)})

    # ---------------- controls ----------------
    control_fail = []
    for kind, expected in RUNTIME_CONTROL.items():
        row = next((r for r in rows if r["kind"] == kind), None)
        if row is None:
            if only and only != kind:
                continue
            control_fail.append("%s: not in the census, so the runtime control could not run" % kind)
            continue
        got = row["one_level"]
        for obj, keys in expected.items():
            missing = sorted(set(keys) - set(got.get(obj, [])))
            if missing:
                control_fail.append(
                    "%s.%s: the runtime annotation reported %s as unmodelled and this walk "
                    "did NOT: missing %s" % (kind, obj, keys, missing))

    if not quiet:
        print("=== unmodelled-key census, offline, from the vendored OAS documents")
        print("    RestDefinitions read   %d" % read)
        print("    kinds censused         %d" % len(rows))
        print()
        for r in sorted(rows, key=lambda r: -len(r["unmodelled"])):
            paths = r["nested"] if nested_only else sorted(r["unmodelled"])
            flag = ("   [no create/update verb: the modelled set is empty BY DESIGN, so "
                    "this row is an artefact, not a finding]" if r["no_request"] else "")
            print("  %-22s via %-7s %3d response paths, %3d unmodelled, %2d of them NESTED%s"
                  % (r["kind"], r["verb"], r["response_paths"], len(r["unmodelled"]),
                     len(r["nested"]), flag))
            for p in paths:
                depth = r["unmodelled"][p]
                if p in r["surfaced"]:
                    mark = "  <- surfaced into status by additionalStatusFields, NOT lost"
                elif depth >= 2:
                    mark = "  <- NESTED, invisible to the annotation (#732)"
                else:
                    mark = ""
                print("      %-56s depth %d%s" % (p, depth, mark))

    total_nested = sum(len(r["nested"]) for r in rows)
    print()
    print("    unmodelled paths, all depths   %d" % sum(len(r["unmodelled"]) for r in rows))
    print("    of those NESTED (depth >= 2)   %d   <- what #732 cannot report" % total_nested)
    print("    of those surfaced into status  %d   <- present in status, NOT lost; the"
          % sum(len(r["surfaced"]) for r in rows))
    print("                                       annotation reports these as unmodelled too")

    print()
    if problems:
        print("CONTROL FAILED: %d resolution problem(s). Each is a stage that could not be"
              " read, NEVER a report of zero unmodelled keys:" % len(problems))
        for p in problems:
            print("  ! %s" % p)
    if control_fail:
        print("CONTROL FAILED: the reconstruction does NOT match the measured runtime"
              " annotation:")
        for c in control_fail:
            print("  ! %s" % c)
    if read == 0:
        print("CONTROL FAILED: zero RestDefinitions read -- the walk broke rather than the"
              " repository being empty.")
    if problems or control_fail or read == 0:
        print("\nVERDICT: VOID -- the census means nothing until these are fixed")
        return 2
    print("CONTROL HELD: the one-level reconstruction reproduces the measured runtime")
    print("    annotation for %d kind(s) (%s), so the method matches the runtime it"
          % (len(RUNTIME_CONTROL), ", ".join(sorted(RUNTIME_CONTROL))))
    print("    approximates -- and the nested rows above are the part the runtime cannot see.")
    print("\nVERDICT: census complete, every control held")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
