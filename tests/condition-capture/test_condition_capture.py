#!/usr/bin/env python3
"""Positive control and empty-input tests for scripts/condition_capture.py.

    python3 tests/condition-capture/test_condition_capture.py

The positive control is what shows the detector DETECTS: a fixture with one child per
outcome, each asserted by name. The empty-input tests are what show the guard can FIRE:
every "nothing found" branch is driven and must exit 3 naming what was absent.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "..", "scripts", "condition_capture.py")
FIXTURE = os.path.join(HERE, "fixture.json")


def run(path):
    return subprocess.run([sys.executable, "-I", SCRIPT, "--fixture", path],
                          capture_output=True, text=True)


def with_record(rec):
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(rec, f)
    f.close()
    return f.name


def base():
    return copy.deepcopy(json.load(open(FIXTURE)))


class Control(unittest.TestCase):
    def test_verdicts(self):
        p = run(FIXTURE)
        self.assertEqual(p.returncode, 0, p.stderr)
        blocks = {}
        cur = None
        for ln in p.stdout.splitlines():
            if ln.startswith("  child "):
                cur = ln.split()[1].rstrip(":").rsplit("/", 1)[-1]
                blocks[cur] = []
            elif cur:
                blocks[cur].append(ln)
        txt = {k: "\n".join(v) for k, v in blocks.items()}
        self.assertEqual(len(txt), 4, "all four fixture children must be reported")
        # the two the lead asked for
        self.assertIn("PROPAGATED (", txt["vpc-prop"])
        self.assertNotIn("NOT PROPAGATED", txt["vpc-prop"])
        self.assertIn("INVISIBLE TO PARENT", txt["lb-syncedonly"])
        # D and P, with the relation stated, on every flap
        for k, v in txt.items():
            self.assertRegex(v, r"D=<?[\d.]+s\s+P=[\d.]+s", k)
            self.assertRegex(v, r"relation: D (<|>=) P", k)
        # the distinction that is the deliverable
        self.assertIn("NOT SAMPLED", txt["subnet-notsampled"])
        self.assertNotIn("NOT PROPAGATED", txt["subnet-notsampled"])
        self.assertIn("NOT PROPAGATED", txt["sg-notprop"])
        self.assertNotIn("NOT SAMPLED", txt["sg-notprop"])


class EmptyInputMustFail(unittest.TestCase):
    def expect_fail(self, rec, needle):
        p = run(with_record(rec))
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("NOTHING FOUND", p.stderr)
        self.assertIn(needle, p.stderr)
        self.assertEqual(p.stdout, "", "a failure must print no report")

    def test_no_samples(self):
        r = base(); r["samples"] = []
        self.expect_fail(r, "0 samples")

    def test_one_sample(self):
        r = base(); r["samples"] = r["samples"][:1]
        self.expect_fail(r, "1 sample")

    def test_no_compositions(self):
        r = base()
        for s in r["samples"]:
            s["compositions"] = []
        self.expect_fail(r, "no compositions")

    def test_no_children(self):
        r = base()
        for s in r["samples"]:
            for c in s["compositions"]:
                c["children"] = {}
        self.expect_fail(r, "0 children")

    def test_no_cdc_lines(self):
        r = base(); r["cdc_log_lines"] = []
        self.expect_fail(r, "no cdc log lines")

    def test_cdc_lines_without_timestamps(self):
        r = base(); r["cdc_log_lines"] = ["reconciling composition chain"]
        self.expect_fail(r, "no cdc log lines")


class ManagedState(unittest.TestCase):
    def kids_gone(self, state):
        r = base()
        for s in r["samples"]:
            for c in s["compositions"]:
                c["children"] = {}
                c["managed_state"] = state
        return run(with_record(r))

    def test_absent_is_named(self):
        p = self.kids_gone("absent")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("status.managed is ABSENT", p.stderr)
        self.assertIn("InventoryNotRecorded", p.stderr)
        self.assertNotIn("EMPTY", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_empty_is_named(self):
        p = self.kids_gone("empty")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("status.managed is present but EMPTY", p.stderr)
        self.assertNotIn("ABSENT", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_notfound_child_is_kept_and_mismatch_stated(self):
        r = base()
        for s in r["samples"][5:9]:
            for c in s["compositions"]:
                k = "vpcs.network.arubacloud.com/ns/lb-syncedonly"
                c["children"][k] = {"absent": True, "ready": {"status": "Absent",
                                    "reason": "NotFound"}, "synced": {"status": "Absent"}}
                c["managed_recorded_but_absent"] = [k]
        p = run(with_record(r))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r"MISMATCH: recorded in status.managed but NotFound "
                                   r"live in 4 of 34 sample\(s\): .*lb-syncedonly")

    def test_sampler_mirrors_rollup_rules(self):
        sys.path.insert(0, os.path.join(HERE, "..", "..", "scripts"))
        import condition_capture as cc

        calls = []

        class R:  # a kubectl result
            def __init__(self, rc, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        class FakeKube:
            def json(self, *a):
                return {"metadata": {"name": "chain", "namespace": "ns"}, "status": {
                    "managedChildrenCount": 3, "managed": [
                        {"apiVersion": "network.arubacloud.com/v1", "resource": "vpcs",
                         "name": "ok", "namespace": "ns"},
                        {"apiVersion": "network.arubacloud.com/v1", "resource": "vpcs",
                         "name": "gone", "namespace": "ns"},
                        {"apiVersion": "network.arubacloud.com/v1", "resource": "",
                         "name": "no-resource"},
                        {"apiVersion": "a/b/c", "resource": "x", "name": "bad-api"}]}}

            def run(self, *a, **k):
                calls.append(a)
                if "gone" in a:
                    return R(1, err='Error from server (NotFound): vpcs "gone" not found')
                return R(0, json.dumps({"kind": "Vpc", "metadata": {"name": "ok"}}))

        out = cc.sample_once(FakeKube(), [("comps.composition.krateo.io", "ns", "chain")])
        c = out["compositions"][0]
        fetched = [x[2] for x in calls]
        self.assertEqual(fetched, ["ok", "gone"], "malformed entries must not be GET")
        self.assertEqual(c["managed_state"], "present")
        self.assertEqual(len(c["managed_skipped_no_get"]), 2)
        self.assertEqual(len(c["managed_recorded_but_absent"]), 1)
        gone = [v for k, v in c["children"].items() if k.endswith("/gone")][0]
        self.assertTrue(gone["absent"])
        self.assertEqual(gone["ready"]["status"], "Absent")


FAKE_KUBECTL = """#!/bin/sh
# Stands in for kubectl: the composition CRD exists, and listing it returns no items.
case "$*" in
  *"get crd"*) echo '{"items":[{"spec":{"group":"composition.krateo.io","names":{"plural":"comps"}}}]}' ;;
  *"get comps"*) echo '{"items":[]}' ;;
  *) echo "unexpected kubectl call: $*" >&2; exit 9 ;;
esac
"""


class CrdExistsZeroInstances(unittest.TestCase):
    def test_exit_3_naming_crd_and_zero_instances(self):
        d = tempfile.mkdtemp()
        k = os.path.join(d, "kubectl")
        open(k, "w").write(FAKE_KUBECTL)
        os.chmod(k, 0o755)
        env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
        p = subprocess.run([sys.executable, "-I", SCRIPT, "--context", "x",
                            "--duration", "1", "--interval", "1"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertIn("NOTHING FOUND", p.stderr)
        self.assertIn("CRD(s) exist", p.stderr)
        self.assertIn("comps.composition.krateo.io", p.stderr)
        self.assertIn("0 instances", p.stderr)
        self.assertEqual(p.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
