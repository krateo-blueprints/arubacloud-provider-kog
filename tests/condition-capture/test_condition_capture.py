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
