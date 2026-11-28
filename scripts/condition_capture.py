#!/usr/bin/env python3
"""Record parent/child condition behaviour during a chain run, so a FLAP can be told
apart from a SAMPLING GAP.

The question this answers: a child went Ready=True -> False/Creating -> True and the
parent Composition never showed it. Did the flap fail to propagate, or did the parent
simply not run while it lasted? The parent only re-evaluates its children once per pass
(P, roughly the resync), so a flap shorter than P can be invisible without anything being
wrong. Those two outcomes must never share a label:

    D <  P and no parent pass fell inside the flap  ->  NOT SAMPLED
    D >= P (or a pass did fall inside) and no trace ->  NOT PROPAGATED

Modes
  live      python3 scripts/condition_capture.py --context CTX [--duration 600 --interval 5]
  analyse   python3 scripts/condition_capture.py --fixture FILE     (offline, no cluster)

Live mode uses the KUBECONFIG in the environment and never touches another one. It only
READS the cluster (get, logs). Pass --capture-out FILE to keep the raw record, which
`--fixture` can re-analyse later.

What is recorded, per sample
  parent  Ready status/reason/message/lastTransitionTime, status.unevaluatedChildren,
          status.managedChildrenCount, metadata.generation vs status.observedGeneration,
          metadata.resourceVersion, status.writtenBy
  child   Ready status/reason/message, Synced status/reason, lastTransitionTime of each,
          metadata.resourceVersion
Once per run: the parent's Events; the composition-dynamic-controller (cdc) log lines that
mention the composition (their timestamps ARE the parent's pass times); and a provenance
block (RDC + cdc image tags and the unstructured-runtime each embeds, core-provider
version, the resync period actually configured).

CHILDREN are read from the parent's `status.managed` (apiVersion/resource/name/namespace),
the list core-provider writes. A parent without it has no discoverable children, which is
reported as an error, not as "no flaps".

VENDOR GET COUNTS are deliberately NOT produced. See VENDOR_GETS below.

Exit codes
  0  ran, found input, report produced
  2  could not ask the cluster / bad arguments
  3  NOTHING FOUND (no compositions, no children, no cdc log lines, too few samples).
     The message names what was absent. This script never prints a clean empty report.
"""
import argparse
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

# Declared expectations. Every count is compared to one of these, never to itself.
MIN_COMPOSITIONS = 1
MIN_CHILDREN = 1
MIN_SAMPLES = 2          # one sample cannot show a transition
MIN_CDC_LINES = 1
MIN_PASSES_FOR_P = 2     # a pass interval needs two passes

# cdc may log several lines for one pass; lines closer than this are one pass.
DEFAULT_PASS_GAP_S = 5.0

VENDOR_GETS = """\
Vendor GET counts per created resource: NOT REPORTED. No reliable source exists.
  * rest-dynamic-controller logs no per-request line. The only per-request output is
    httputil.DumpRequestOut/DumpResponse (restclient.go debuggingRoundTripper), gated by
    the per-CR `krateo.io/connector-verbose` annotation, written to stdout with no
    timestamp and no CR name; list/findby GETs carry no resource id. Counting it would
    need that annotation set on every CR (mutating the run) and attributing by URL
    substring, which undercounts findby. (Below 0.26.1 that dump prints the bearer.)
  * REST_CONTROLLER_DEBUG / OASGEN_PROVIDER_DEBUG switch slog debug lines, not the dump.
  * The metrics server (REST_CONTROLLER_METRICS_SERVER_PORT) exposes client-go's
    rest_client_requests_total, which counts Kubernetes-API calls; the vendor client is a
    separate http.Client and is not in it.
  A number from any of these would be one this script cannot defend."""


class NothingFound(Exception):
    """Raised, never swallowed: the input was empty where it must not be."""


# ---------------------------------------------------------------- time helpers
def parse_ts(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip()
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?$", s)
    if not m:
        raise ValueError(f"unparseable timestamp {s!r}")
    base = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    tz = m.group(3)
    if tz in (None, "Z"):
        off = 0
    else:
        sign = 1 if tz[0] == "+" else -1
        off = sign * (int(tz[1:3]) * 3600 + int(tz[4:6]) * 60)
    frac = float(m.group(2)) if m.group(2) else 0.0
    return base.replace(tzinfo=dt.timezone.utc).timestamp() - off + frac


# ------------------------------------------------------------ object summarisers
def cond(obj, typ):
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == typ:
            return {"status": c.get("status"), "reason": c.get("reason"),
                    "message": c.get("message"),
                    "lastTransitionTime": c.get("lastTransitionTime")}
    return None


def summarise_parent(obj):
    st = obj.get("status") or {}
    md = obj.get("metadata") or {}
    return {
        "name": md.get("name"), "namespace": md.get("namespace"), "uid": md.get("uid"),
        "ready": cond(obj, "Ready"),
        "unevaluatedChildren": st.get("unevaluatedChildren"),
        "managedChildrenCount": st.get("managedChildrenCount"),
        "generation": md.get("generation"),
        "observedGeneration": st.get("observedGeneration"),
        "resourceVersion": md.get("resourceVersion"),
        "writtenBy": st.get("writtenBy"),
    }


def summarise_child(obj):
    md = obj.get("metadata") or {}
    return {
        "kind": obj.get("kind"), "name": md.get("name"),
        "ready": cond(obj, "Ready"), "synced": cond(obj, "Synced"),
        "resourceVersion": md.get("resourceVersion"),
    }


# ------------------------------------------------------------------ kubectl I/O
class Kube:
    def __init__(self, context):
        self.base = ["kubectl"] + (["--context", context] if context else [])

    def run(self, *args, check=True):
        p = subprocess.run(self.base + list(args), capture_output=True, text=True)
        if check and p.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} -> exit {p.returncode}: "
                               f"{p.stderr.strip()}")
        return p

    def json(self, *args):
        return json.loads(self.run(*args, "-o", "json").stdout)


def discover_compositions(kube):
    """Every instance of every CRD in group composition.krateo.io."""
    crds = kube.json("get", "crd")["items"]
    plurals = sorted(c["spec"]["names"]["plural"] + "." + c["spec"]["group"]
                     for c in crds if c["spec"]["group"] == "composition.krateo.io")
    if not plurals:
        raise NothingFound("no CustomResourceDefinition in group composition.krateo.io "
                           "exists on this cluster, so there can be no Composition")
    found = []
    for res in plurals:
        for it in kube.json("get", res, "-A")["items"]:
            found.append((res, it["metadata"].get("namespace", ""), it["metadata"]["name"]))
    if len(found) < MIN_COMPOSITIONS:
        raise NothingFound(
            f"no compositions found: {len(plurals)} composition CRD(s) exist "
            f"({', '.join(plurals)}) but they hold 0 instances (expected >= "
            f"{MIN_COMPOSITIONS})")
    return found


def managed_refs(parent_obj):
    refs = []
    for m in (parent_obj.get("status") or {}).get("managed") or []:
        grp = (m.get("apiVersion") or "").split("/")[0]
        if m.get("resource") and m.get("name"):
            res = m["resource"] + ("." + grp if "/" in (m.get("apiVersion") or "") else "")
            refs.append((res, m.get("namespace", ""), m["name"]))
    return refs


def sample_once(kube, comps):
    now = time.time()
    out = []
    for res, ns, name in comps:
        nsarg = ["-n", ns] if ns else []
        pobj = kube.json("get", res, name, *nsarg)
        children = {}
        for cres, cns, cname in managed_refs(pobj):
            cnsarg = ["-n", cns] if cns else []
            p = kube.run("get", cres, cname, *cnsarg, "-o", "json", check=False)
            key = f"{cres}/{cns}/{cname}"
            if p.returncode != 0:
                children[key] = {"error": p.stderr.strip()}
            else:
                children[key] = summarise_child(json.loads(p.stdout))
        out.append({"composition": f"{res}/{ns}/{name}", "parent": summarise_parent(pobj),
                    "children": children})
    return {"t": now, "compositions": out}


def tag_of(image):
    base = image.rsplit("/", 1)[-1]
    return base.split(":", 1)[1].split("@")[0] if ":" in base else None


def go_mod_runtime_version(repo, tag):
    """unstructured-runtime version from the go.mod at the image's git tag. The image does
    not carry it in a readable place, and the binary is distroless."""
    for ref in (tag, "v" + tag if not tag.startswith("v") else tag[1:]):
        url = f"https://raw.githubusercontent.com/krateoplatformops/{repo}/{ref}/go.mod"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                m = re.search(r"unstructured-runtime\s+(v\S+)", r.read().decode())
                if m:
                    return m.group(1)
        except Exception:
            continue
    return None


def pods_by_image(kube, needle):
    pods = kube.json("get", "pods", "-A")["items"]
    return [p for p in pods
            if any(needle in c.get("image", "")
                   for c in p["spec"].get("containers", []))]


def configured_resync(pod, env_name):
    """Resync as the pod is ACTUALLY configured: a -resync-interval arg wins over the
    env var, which wins over the code default. Reports where the value came from."""
    for c in pod["spec"]["containers"]:
        for a in (c.get("args") or []) + (c.get("command") or []):
            m = re.match(r"^-{1,2}resync-interval[= ](.+)$", a)
            if m:
                return {"value": m.group(1), "source": f"arg {a}"}
        args = (c.get("args") or [])
        for i, a in enumerate(args):
            if re.match(r"^-{1,2}resync-interval$", a) and i + 1 < len(args):
                return {"value": args[i + 1], "source": f"arg {a} {args[i+1]}"}
        for e in c.get("env") or []:
            if e.get("name") == env_name:
                if "value" in e:
                    return {"value": e["value"], "source": f"env {env_name}"}
                return {"value": None, "source": f"env {env_name} via valueFrom "
                                                  "(not resolved)"}
    return {"value": "3m", "source": "code default (no arg or env set)"}


def build_provenance(kube, comp_summary_first):
    prov = {}
    for key, needle, repo, env_name in (
            ("rdc", "rest-dynamic-controller", "rest-dynamic-controller",
             "REST_CONTROLLER_RESYNC_INTERVAL"),
            ("cdc", "composition-dynamic-controller", "composition-dynamic-controller",
             "COMPOSITION_CONTROLLER_RESYNC_INTERVAL")):
        pods = pods_by_image(kube, needle)
        if not pods:
            prov[key] = {"error": f"no pod runs an image matching {needle!r}"}
            continue
        images = sorted({c["image"] for p in pods for c in p["spec"]["containers"]
                         if needle in c["image"]})
        tags = sorted({tag_of(i) or "?" for i in images})
        rt = {t: go_mod_runtime_version(repo, t) for t in tags if t != "?"}
        prov[key] = {"images": images, "tags": tags,
                     "unstructured_runtime": {t: (v or "UNRESOLVED (go.mod not fetched)")
                                              for t, v in rt.items()},
                     "resync": configured_resync(pods[0], env_name),
                     "pods": len(pods)}
    wb = (comp_summary_first or {}).get("writtenBy") or {}
    if wb.get("version"):
        prov["core_provider"] = {"version": wb["version"],
                                 "source": "status.writtenBy.version"}
    else:
        prov["core_provider"] = {
            "version": None,
            "source": "status.writtenBy is ABSENT on the composition (it exists only "
                      "from core-provider 2.13.135); read the version from the "
                      "core-provider chart/image on the cluster instead"}
    return prov


def capture_cdc_lines(kube, comp_names, since):
    pods = pods_by_image(kube, "composition-dynamic-controller")
    if not pods:
        raise NothingFound("no composition-dynamic-controller pod found on the cluster; "
                           "there are no cdc log lines to read parent pass times from")
    lines = []
    for p in pods:
        ns, nm = p["metadata"]["namespace"], p["metadata"]["name"]
        r = kube.run("logs", "-n", ns, nm, "--timestamps", f"--since={since}s",
                     check=False)
        for ln in r.stdout.splitlines():
            if any(n in ln for n in comp_names):
                lines.append(ln)
    return lines


def capture_events(kube, comps):
    ev = []
    for res, ns, name in comps:
        nsarg = ["-n", ns] if ns else ["-A"]
        d = kube.json("get", "events", *nsarg, "--field-selector",
                      f"involvedObject.name={name}")
        ev += [{"composition": f"{res}/{ns}/{name}", "type": e.get("type"),
                "reason": e.get("reason"), "message": e.get("message"),
                "lastTimestamp": e.get("lastTimestamp") or e.get("eventTime")}
               for e in d["items"]]
    return ev


def live_capture(a):
    kube = Kube(a.context)
    comps = discover_compositions(kube)
    started = time.time()
    samples = []
    end = started + a.duration
    while True:
        samples.append(sample_once(kube, comps))
        if time.time() + a.interval > end:
            break
        time.sleep(a.interval)
    first = samples[0]["compositions"][0]["parent"] if samples[0]["compositions"] else {}
    cdc = capture_cdc_lines(kube, [c[2] for c in comps],
                            int(time.time() - started) + 60)
    return {"provenance": build_provenance(kube, first), "samples": samples,
            "events": capture_events(kube, comps), "cdc_log_lines": cdc}


# ---------------------------------------------------------------------- analysis
def cdc_times(lines):
    ts = []
    for ln in lines:
        m = re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", ln)
        if m:
            ts.append(parse_ts(m.group(0)))
    return sorted(ts)


def passes_from(times, gap):
    passes = []
    for t in times:
        if not passes or t - passes[-1] >= gap:
            passes.append(t)
    return passes


def series(samples, comp, child):
    out = []
    for s in samples:
        for c in s["compositions"]:
            if c["composition"] == comp and child in c["children"]:
                out.append((s["t"], c["children"][child], c["parent"]))
    return out


def find_flaps(rows, which):
    """Windows during which condition `which` was not True, bracketed by True on both
    sides. start/end come from lastTransitionTime where present (the controller's own
    record, finer than the sample interval) else from the sample times.

    A flap that began AND ended between two samples leaves status True on both, but moves
    lastTransitionTime. It is reported with `bound` set: its start is unknown, so D is an
    UPPER bound (end minus the earlier sample), never a measurement."""
    flaps, cur, seen_true, prev = [], None, False, None
    for t, ch, _ in rows:
        c = ch.get(which) or {}
        if c.get("status") == "True":
            ltt = c.get("lastTransitionTime")
            if cur is not None:
                cur["end"] = parse_ts(ltt) if ltt else t
                flaps.append(cur)
                cur = None
            elif prev is not None and ltt and ltt != prev[1] and prev[0] is not None:
                flaps.append({"which": which, "start": prev[0], "end": parse_ts(ltt),
                              "bound": True, "status": "hidden", "reasons": {"(hidden)"},
                              "messages": set()})
            seen_true, prev = True, (t, ltt)
        elif seen_true:
            if cur is None:
                cur = {"which": which, "start": t, "bound": False,
                       "status": c.get("status"), "reasons": set(), "messages": set()}
            cur["reasons"].add(c.get("reason"))
            if c.get("message"):
                cur["messages"].add(c["message"])
            if c.get("lastTransitionTime"):
                lt = parse_ts(c["lastTransitionTime"])
                if lt <= cur["start"]:
                    cur["start"] = lt
            prev = None
    return flaps


def parent_evidence(rows, start, end, P):
    """Did the PARENT's own condition show anything inside [start, end + P]?"""
    for t, _, par in rows:
        r = par.get("ready") or {}
        if start <= t <= end + (P or 0) and r.get("status") != "True":
            return f"parent Ready={r.get('status')}/{r.get('reason')} sampled at +{t-start:.0f}s"
        ltt = r.get("lastTransitionTime")
        if ltt and start <= parse_ts(ltt) <= end + (P or 0):
            return f"parent Ready lastTransitionTime {ltt} falls in the window"
    return None


def classify(flap, rows, passes, P):
    D = flap["end"] - flap["start"]
    bound = flap.get("bound", False)
    in_win = [p for p in passes if flap["start"] <= p <= flap["end"]]
    rel = None
    if P is None:
        rel = "D vs P: UNDEFINED (fewer than 2 parent passes observed)"
    else:
        rel = (("D < P" if D < P else "D >= P") +
               f"  (D={'<=' if bound else ''}{D:.1f}s, P={P:.1f}s)")
        if bound and D >= P:
            rel += "  [D is only an upper bound: start fell between samples]"
    ready_down = any((ch.get("ready") or {}).get("status") != "True"
                     for t, ch, _ in rows if flap["start"] <= t <= flap["end"])
    if flap["which"] == "synced" and not ready_down:
        verdict = ("INVISIBLE TO PARENT: the child's Ready stayed True; only Synced "
                   "flapped, and the parent decides on Ready")
    else:
        ev = parent_evidence(rows, flap["start"], flap["end"], P)
        if ev:
            verdict = f"PROPAGATED ({ev})"
        elif P is None:
            verdict = "INDETERMINATE (no pass interval to compare D against)"
        elif not in_win and D < P:
            verdict = ("NOT SAMPLED: D < P and no parent pass fell inside the flap; the "
                       "parent never had the chance to observe it. This is NOT a "
                       "propagation failure")
        elif bound and D >= P:
            verdict = ("INDETERMINATE: the flap began between samples, so D is only an "
                       "upper bound that is not below P; shorten --interval")
        elif not in_win:
            verdict = ("INDETERMINATE: D >= P yet the pass log shows no pass inside the "
                       "window; the cdc log has a gap, so neither 'not sampled' nor "
                       "'not propagated' can be claimed")
        else:
            verdict = (f"NOT PROPAGATED: {len(in_win)} parent pass(es) fell inside the "
                       "flap and the parent showed nothing")
    return {"D": D, "relation": rel, "passes_in_window": len(in_win), "verdict": verdict}


def analyse(rec, pass_gap=DEFAULT_PASS_GAP_S):
    samples = rec.get("samples") or []
    if not samples:
        raise NothingFound("the record holds 0 samples")
    if len(samples) < MIN_SAMPLES:
        raise NothingFound(f"the record holds {len(samples)} sample(s); at least "
                           f"{MIN_SAMPLES} are needed to see any transition")
    comps = sorted({c["composition"] for s in samples for c in s["compositions"]})
    if len(comps) < MIN_COMPOSITIONS:
        raise NothingFound(f"no compositions in any sample (found {len(comps)}, "
                           f"expected >= {MIN_COMPOSITIONS})")
    lines = rec.get("cdc_log_lines") or []
    times = cdc_times(lines)
    if len(times) < MIN_CDC_LINES:
        raise NothingFound(f"no cdc log lines mentioning the composition(s) with a "
                           f"parseable timestamp ({len(lines)} line(s) given, "
                           f"{len(times)} timestamped; expected >= {MIN_CDC_LINES}); "
                           "parent pass times cannot be derived")
    passes = passes_from(times, pass_gap)
    P = None
    if len(passes) >= MIN_PASSES_FOR_P:
        P = statistics.median(b - a for a, b in zip(passes, passes[1:]))
    report = {"passes_observed": len(passes), "P": P, "compositions": {}, "children": 0}
    for comp in comps:
        kids = sorted({k for s in samples for c in s["compositions"]
                       if c["composition"] == comp for k in c["children"]})
        if len(kids) < MIN_CHILDREN:
            raise NothingFound(f"composition {comp} has 0 children in every sample "
                               f"(expected >= {MIN_CHILDREN}); its status.managed is "
                               "empty or absent")
        rvs = [c["parent"].get("resourceVersion") for s in samples
               for c in s["compositions"] if c["composition"] == comp]
        entry = {"parent_writes_seen": len(set(rvs)) - 1, "children": {}}
        for k in kids:
            rows = series(samples, comp, k)
            flaps = find_flaps(rows, "ready") + find_flaps(rows, "synced")
            entry["children"][k] = {
                "samples": len(rows),
                "child_writes_seen": len({r[1].get("resourceVersion") for r in rows}) - 1,
                "flaps": [dict(f, **classify(f, rows, passes, P)) for f in flaps]}
            report["children"] += 1
        report["compositions"][comp] = entry
    if report["children"] < MIN_CHILDREN:
        raise NothingFound("no children found")
    return report


def render(rec, rep):
    o = []
    o.append("== provenance ==")
    for k, v in (rec.get("provenance") or {}).items():
        o.append(f"  {k}: {json.dumps(v, sort_keys=True)}")
    o.append("== parent passes (from cdc log timestamps) ==")
    P = rep["P"]
    o.append(f"  passes observed: {rep['passes_observed']}   "
             f"P (median interval): {'%.1fs' % P if P is not None else 'UNDEFINED'}")
    o.append(f"== vendor GETs ==\n  {VENDOR_GETS.splitlines()[0]} (reason: see --help)")
    for comp, e in rep["compositions"].items():
        o.append(f"== {comp}   parent writes seen: {e['parent_writes_seen']} ==")
        for k, c in e["children"].items():
            o.append(f"  child {k}: {c['samples']} samples, "
                     f"{c['child_writes_seen']} writes seen, {len(c['flaps'])} flap(s)")
            for f in c["flaps"]:
                o.append(f"    flap on {f['which']}: reasons={sorted(map(str, f['reasons']))}"
                         f"  D={'<=' if f.get('bound') else ''}{f['D']:.1f}s  P={'%.1fs' % P if P is not None else 'n/a'}"
                         f"  passes observed={rep['passes_observed']}"
                         f"  passes in window={f['passes_in_window']}")
                o.append(f"      relation: {f['relation']}")
                o.append(f"      verdict:  {f['verdict']}")
    return "\n".join(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog=VENDOR_GETS,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default=os.environ.get("KUBE_CONTEXT"))
    ap.add_argument("--duration", type=float, default=600)
    ap.add_argument("--interval", type=float, default=5,
                    help="seconds between samples (default 5). A flap that starts and "
                         "ends between two samples only gets an upper bound on D; if "
                         "that bound is not below P the verdict is INDETERMINATE and "
                         "the remedy is a smaller --interval. P comes ONLY from cdc log "
                         "pass timestamps, with no fallback to the configured resync")
    ap.add_argument("--pass-gap", type=float, default=DEFAULT_PASS_GAP_S,
                    help="cdc log lines closer than this are one parent pass")
    ap.add_argument("--fixture", help="analyse this recorded JSON instead of a cluster")
    ap.add_argument("--capture-out", help="write the raw record here")
    a = ap.parse_args(argv)
    try:
        if a.fixture:
            rec = json.load(open(a.fixture))
        else:
            if not a.context:
                print("ERROR: pass --context (or KUBE_CONTEXT); refusing to use the "
                      "current-context of whatever kubeconfig is ambient", file=sys.stderr)
                return 2
            rec = live_capture(a)
        if a.capture_out:
            json.dump(rec, open(a.capture_out, "w"), indent=1, default=str)
        rep = analyse(rec, a.pass_gap)
    except NothingFound as e:
        print(f"NOTHING FOUND: {e}", file=sys.stderr)
        return 3
    except (RuntimeError, OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    print(render(rec, rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
