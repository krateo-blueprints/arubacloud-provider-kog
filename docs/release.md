---
type: Runbook
title: krateo-arubacloud-provider-kog — release
description: How the aruba-network-environment Composition chart is packaged and published as an OCI artifact, how the CompositionDefinition version tracks it, and how the generated manifests are refreshed from updated Aruba specs.
resource: oci://ghcr.io/krateo-blueprints/charts/aruba-network-environment
tags: [release, oci, ghcr, chart, regeneration]
timestamp: 2026-09-05T00:00:00Z
---

# Release

This repository is a blueprint, not a compiled component. Two things ship: the
**Composition chart** (as an OCI artifact) and the **generated manifests** (via
the repository itself). There is no application image.

## Publishing the Composition chart

The `aruba-network-environment` chart under `compositions/` is packaged and
pushed to GHCR as an OCI artifact. The `CompositionDefinition` points at it:

```yaml
spec:
  chart:
    url: oci://ghcr.io/krateo-blueprints/charts/aruba-network-environment
    version: "0.1.0"
```

## Versioning

**The repository is versioned by git tag, and the tag is the only source of truth.**

`.github/workflows/release-tag.yaml` fires on any tag matching `[0-9]+.[0-9]+.[0-9]+`.
It discovers every `Chart.yaml`, substitutes the `CHART_VERSION` placeholder with the
tag, and publishes each chart as an OCI artifact:

```
oci://ghcr.io/krateo-blueprints/charts/<chart-name>:<tag>
```

No chart carries a hand-written version — `CHART_VERSION` is a placeholder precisely so
a version can never be committed, drift from the tag, or be bumped in one file and
forgotten in another. `scripts/validate.py` lints a copy with the placeholder
substituted, so `helm lint` still passes on an unstamped tree.

### What the number means here

This blueprint's version tracks **the evidence, not the code**. The manifests are
generated and change only when Aruba's specs do; what actually moves is how much of the
surface has been proven against the live API. So:

| Bump | When |
|------|------|
| **patch** | regenerated manifests, doc corrections, more resources proven at an existing tier |
| **minor** | a resource changes tier, a new upstream provider version is adopted, or a chain/runner capability lands |
| **major** | reserved for a breaking change to a generated CRD — which happens when Aruba changes `info.version`, since the CRD version is derived from it (`1.0.0` → `v1-0-0`) |

That last row is the one to watch: an `info.version` bump upstream renames the served
CRD version and orphans existing CRs. `.github/workflows/oas-drift.yaml` exists to catch
it before a user does.

**A generated CRD can also break for a reason of OUR making, which that row did not
anticipate.** Adding an entry to `excludedSpecFields` removes a property from the generated
spec: a manifest that set it is rejected, and whatever live CRs held there is pruned and not
recoverable. `Kaas.clientSecret` in 0.4.0 is the first instance — a plaintext credential, so the
pruning *is* the remedy, which is what made it worth doing rather than a regression.

It is **not** treated as a major bump, deliberately: the repository is pre-1.0 (see below), where
a minor may carry a breaking change, and reserving major for `info.version` keeps that signal
meaning one specific thing. What the release notes must do instead is say so explicitly — a user
upgrading cannot discover a removed spec property from a version number. The migration is in
[Authentication](authentication.md), including the measured apiserver refusal and why the
delete-and-recreate blocks while instances are live.

### Pre-1.0

The provider is **pre-1.0 and will stay there until the GA core is closed**. At the time
of writing 27 of 35 resources are GA, 2 beta, 4 experimental and 2 blocked — the tier
table in [coverage](coverage.md) is generated from recorded evidence, and
[ga-readiness](ga-readiness.md) lists what remains. Publishing 1.0.0 while any
resource cannot complete its lifecycle would misrepresent the thing, and two cannot:
`Image` creates but cannot be deleted, and `Hpc` cannot be read back at all.

Note those are not the same two as the `blocked` tier, which holds `Image` and
`LoadBalancer`. `LoadBalancer` is blocked **by design** — Aruba exposes no create
for it, confirmed in their own SDK — so it is a deliberate read-only resource, not
a broken one. `Hpc` sits at `experimental` and is the genuinely broken one, because
its collection 404s in every project. Tier and brokenness are different axes here.

Manual publish, for a one-off outside CI — substitute the version yourself, since
the tree carries placeholders rather than numbers:

```sh
VERSION=0.2.0
helm package compositions/aruba-network-environment --version "$VERSION"
helm push "aruba-network-environment-${VERSION}.tgz" \
  oci://ghcr.io/krateo-blueprints/charts
```

Both `compositions/aruba-network-environment/Chart.yaml` and
`spec.chart.version` in `compositions/compositiondefinition.yaml` hold the literal
`CHART_VERSION`, which the tag workflow substitutes, so there is nothing to keep in
sync by hand and no number to forget. A chart-version bump still changes the
Composition's derived apiVersion/GVK — that is a property of the release, not an
edit someone makes.

## Refreshing the generated manifests

Everything under `openapi/`, `configmaps/`, `restdefinitions/`, `samples/` and
`docs/coverage.md`/`docs/providers/` is generated. To refresh from updated Aruba
specs:

```sh
# 1. refresh openapi/*.json from https://arubacloud.github.io/api/openapi/ (vendored
#    UNMODIFIED -- update openapi/CHECKSUMS.txt alongside; validate.py enforces it), then:
python3 scripts/generate_restdefinitions.py  # -> restdefinitions/
python3 scripts/gen_configmaps.py            # -> configmaps/
python3 scripts/gen_samples_and_coverage.py  # -> samples/ + docs/coverage.md
python3 scripts/gen_provider_docs.py         # -> docs/providers/
python3 scripts/validate.py                  # static + helm validation (must pass)
```

The specs are consumed as published -- no OAS rewriting; RestDefinitions adapt to the
contract instead. Any generation gap is a
concrete oasgen-provider gap, analysed in
[oasgen-provider evolution](oasgen-provider-evolution.md). `validate.py` must exit
zero before a release: it checks every RestDefinition verb path+method against its
patched OAS, every `requestFieldMapping.inPath` against the path parameters, every
`oasPath` ConfigMap reference, and `helm lint` + `helm template` on the chart.

## Compatibility

These manifests require **oasgen-provider >= 0.36.0**. That is the FLOOR, not the pin:
the pin is **0.67.0** and is what CI validates (see [Usage](usage.md#prerequisites)). The
two were the same number when this section was written and have not been since; keeping
them distinct matters because the floor explains what cannot be lowered, while the pin is
what the evidence was gathered on. It is no longer a fork:
rest-dynamic-controller and both charts live in the oasgen-provider monorepo, and the
chart derives the RDC image tag from its own `appVersion`, so the two cannot drift
apart.

### Before the next release: bump the chart-push pin

**Open item, recorded here because this is where a release is performed and nowhere else
would stop it.**

Chart pushes go through `krateo-platformops/.github/.github/actions/chart-push`
(krateo-blueprints/charts rule 14), pinned in `release-tag.yaml` at
`5f126b7d93ec5302eb9cc57a3af157550175fbaf`. At that commit `push.sh` reads the manifest
back with the registry credential on **curl's command line** — `-u user:pass` at line 69
and `-H "Authorization: Bearer ..."` at line 73 — so both sit in process argv and are
readable by any other process in the same job. The credential is never echoed or written
to a file and the token is pull-scoped, which bounds it; but it IS an exposure the previous
bare `helm push` did not have, because that path passed the token to
`helm registry login --password-stdin` and never put it in an argument.

A fix is in progress upstream (both reads moved onto `curl -K -` on stdin, with a test
that asserts argv is clean). **Bump the pin to that commit before cutting the next tag**,
and verify the new `push.sh` and `action.yml` sha256 against whatever reports them rather
than taking the SHA on trust — the tag that carries it is annotated, so peel it (`1.4.0^{}`)
rather than reading `git/ref/tags/1.4.0`, which returns the tag object.

This is not urgent in the sense that the code path only runs on a tag push: nothing
executes `push.sh` until a release. It is recorded as a release precondition rather than a
TODO because a release is exactly the moment it would otherwise be discovered.

### Moving the pin: what will stop you, and what to do about it

Three gates, each catching a case the others cannot. None of them existed before
2026-11-18, when the skip list was still a COMMENT in `pin_verify.sh` and
`OASGEN_VERSION=0.66.0` would have installed a version measured as broken and written
down as skipped.

**1. The three written forms must agree.** `scripts/validate.py`
(`verify_pin_sources_agree`) refuses when `OASGEN_VERSION` in
`.github/workflows/validate.yaml`, that file's own `# CURRENT PIN:` header, and
`pin_verify.sh`'s `PIN` default disagree. Offline, so it runs on every PR as *static
validation*. It exists because two of those three had already drifted: the workflow
header read `THE PIN, 0.52.0` while CI pinned 0.67.0.

**2. The producer's retraction list.** `scripts/check_retracted.py` reads
oasgen-provider's own `retracted-versions.yaml` rather than a copy of its contents, so a
retraction published upstream applies here without anyone being told. Enforced in two
places:

* `pin_verify.sh` consults it before building anything, so a local run cannot verify a
  retracted version;
* the **`pin not retracted`** CI job consults it on a pull request *only when a file
  naming the pin changed*, and on **every push to main** regardless. The split is
  deliberate: the script fails closed, and making every PR depend on
  `raw.githubusercontent` would produce a gate that reddens for unrelated reasons.

**3. This repo's own skips**, in `pin_verify.sh`, kept visibly separate from gate 2
because the two say different things — the producer's list says *nobody* should pin this,
ours says *this repo* cannot use it. Currently one entry, 0.53.0, which oasgen
deliberately declined to retract for everyone.

#### When a gate fires

* **RETRACTED (exit 1)** — pin the version the entry names under `use:`. There is no
  override flag, and that is not an oversight: a retraction is the producer stating that
  the version damages consumers, and a flag to ignore it would make the gate decoration.
* **COULD NOT TELL (exit 2)** — the list could not be fetched or parsed. This is **not** a
  pass. "I could not read the retraction list" is not evidence that a version is safe;
  retry when it is reachable.
* **Pin sources disagree** — fix all three, not the one the error names. CI would
  otherwise verify one version while `pin_verify.sh` verified another, and the run that
  says "the pin is good" would not be about the pin CI uses.

#### Known obstacles between 0.67.0 and oasgen's newest

**One thing the bump does NOT change: the Kubernetes floor.** Read from the published
charts rather than inferred, across the whole path including every version the bump would
pass through:

```
0.67.0  kubeVersion: '>=1.36.0-0'      <- the current pin
0.68.0  kubeVersion: '>=1.36.0-0'
0.73.0  kubeVersion: '>=1.36.0-0'
0.76.0  kubeVersion: '>=1.36.0-0'
0.78.0  kubeVersion: '>=1.36.0-0'
0.80.0  kubeVersion: '>=1.36.0-0'
0.82.0  kubeVersion: '>=1.36.0-0'
0.83.1  kubeVersion: '>=1.36.0-0'      <- oasgen's newest
```

So the 1.36 floor documented below survives the bump unchanged, and `NODE_IMAGE` in
`scripts/pin_verify.sh` does not have to move with it. That is worth stating explicitly
because the floor HAS moved with a pin before -- 0.28.0 raised it to 1.36 and forced the
node image up in the same commit -- so "the floor is fine" is a measurement here, not an
assumption.


Live as of 2026-11-18; the measurements are in
[the evolution appendix](oasgen-provider-evolution.md).

| version | why not |
|---|---|
| 0.69.0, 0.70.0 | retracted: a create whose status write loses a race duplicates the external resource. Scoped to kinds with **no** `findby`, which is 0 of our 36 — but retracted for everyone, so the gate refuses them |
| 0.77.0 | retracted: a create whose outcome is unknown stays `Ready=False/Creating` instead of reaching `CreateOutcomeUnknown`. Scoped to findby-addressed kinds, which is **36 of 36** here — our maximum-exposure case |
| 0.83.0 | tagged and never published. Fails at fetch, so it enforces itself |
| below 0.76.0 | a floor **if `partialFields` is ever adopted**: below it the CRD does not declare the field, Kubernetes prunes it, and an update then sends each map WHOLE. 0 of 36 declare it today, so there is no instance — by luck, not design |
| 0.80.0 and above | oasgen#818: a create answering 404 stamps `krateo.io/external-create-pending` and it never clears. Exposure is a FRESH install, which is exactly what a bump-verification cluster is. Fixed only on oasgen's unreleased main (#825) |

**The Kubernetes floor is 1.36.** Measured from the published charts rather than
inferred, re-measured 2026-11-13 across the whole range the pin has travelled:

| oasgen-provider | `kubeVersion` |
|---|---|
| 0.27.1 (a former pin) | `>=1.33.0-0` |
| 0.34.0 | `>=1.36.0-0` |
| 0.36.0 (**the floor**) | `>=1.36.0-0` |
| 0.37.0 | `>=1.36.0-0` |
| 0.61.0 (a former pin) | `>=1.36.0-0` |
| 0.63.1 (a former pin) | `>=1.36.0-0` |
| 0.64.0 (a former pin) | `>=1.36.0-0` |
| 0.65.0 (a former pin) | `>=1.36.0-0` |
| 0.66.0 (**skipped**, see below) | `>=1.36.0-0` |
| **0.67.0 (the pin)** | `>=1.36.0-0` |

So the floor has not moved since 0.34.0, and the pin reaching 0.67.0 costs nothing further
in cluster version. The chart's `appVersion` at 0.67.0 is also `0.67.0` (re-measured 2026-11-14 from
the published chart, as is every row above), which is what ties the rest-dynamic-controller image tag
to the chart rather than letting the two drift.

### What a provider UPGRADE does to 36 CRDs, which a fresh-cluster harness cannot see

A provider upgrade re-renders **every** managed RestDefinition's CRD on its first reconcile after
the upgrade: `generatorChanged` is OR'd with the content hashes in `restdefinition.go` and the
regeneration default is true. For this fleet that is **36 CRD applies in one burst**, not a no-op.
`OASGEN_PROVIDER_REGENERATE_ON_UPGRADE=false` suppresses only the render; Deploy still delivers the
new controller image. And it suppresses only that one clause — a `spec.resource` change moves
`resourceHash`, a separate clause of the same OR, and regenerates regardless of the knob.
The mechanism was reported by the openstack-ironic-operator-kog session, which hit it on 7
definitions; the 36-apply figure is ours.

**A fresh-cluster-per-run harness measures a clean install and says nothing about this path.**
The specific risk it cannot see: a regeneration that unserves a CRD version something still pins.

`generatorChanged` is **directional** (`restdefinition.go:196`): it fires only when this build's
MAJOR.MINOR.PATCH is *greater* than the one recorded in `status.generatorBuild`, so a rollback
leaves the newer schema in place rather than reverting it — and an unparseable stamp fails closed.
An empty `Build` is deliberately inert; an empty *stored* value regenerates once, which is the
activation path every existing cluster takes.

**Measured on the 0.64.0 → 0.65.0 upgrade (2026-11-14), all 36 definitions, released charts:**

```
served-version set after the upgrade is a SUPERSET of the set before : HELD
  kinds that disappeared entirely   0
  kinds that lost a served version  0
  kinds whose storage version moved 0
  CRD JSON byte-identical before vs after  36 of 36
```

So this particular upgrade changed nothing observable in any CRD. Both tags ship **plumbing
v1.60.0**, so the rendered output is identical and the applies are no-ops. **What the run does NOT
establish is whether the re-render ran at all**: a no-op apply does not bump `resourceVersion`, and
the field that records the decision is `status.generatorBuild` on the RestDefinition, which this
run snapshotted the CRDs *affected by* rather than the objects that *record* it. So: effect
measured and nil; occurrence unmeasured. The dangerous case remains an upgrade whose generator
output differs — which is an upgrade that moves plumbing or crdgen, not merely the provider.

The floor rose in chart 0.28.0 (oasgen#178), because version-scoped watching needs a
MutatingAdmissionPolicy that 1.33-1.35 do not serve. On an older cluster Helm refuses
the chart outright. That cost is passed to every user, and it was taken deliberately
for two things that cannot be had otherwise:

* **oasgen#227** — a per-verb `oasPath` finally reaches the controller. This is what
  makes CloudServer's create reachable at all: the create endpoint exists only in
  `compute-provider_v1.1.json`, and below 0.34.0 RDC reads only `spec.oasPath`, so
  the verb fails with `operation not found: POST`.
* **oasgen#225** — an appended `additionalStatusFields` entry now regenerates an
  EXISTING CRD. Below it the append is accepted and silently does nothing, which
  would have left our `status.error` mapping inert on every cluster that already had
  these CRDs.

Two further requirements took the pin from 0.34.0 to **0.36.0**, both about one
resource — `profile/AiKey`, which is generated from `profile.json`:

* **libopenapi v0.21.9**, first shipped in oasgen-provider **0.35.0**. A `+` in a
  schema name (`ApiKeyQuotasDto+QuotaValue`, a .NET nested-type separator) made the
  document unparseable, because `$ref` fragments were unescaped with form-encoding
  semantics that decode `+` as a space. It fails in `BuildV3Model`, before generation
  runs, so below 0.35.0 AiKey does not fail — it **never appears, and nothing says
  why**. Also reported upstream as
  [Arubacloud/api#25](https://github.com/Arubacloud/api/issues/25).
* **oasgen#236**, shipped in **0.36.0**. `aiKeys` has a PUT and no POST, and below
  this the CR's spec body fields were derived from the create verb — so with none,
  the resource was treated as read-only and the PUT sent
  `{"metadata": {"name": "..."}}` against a full-replacement body, silently dropping
  the vendor's `location` and `tags`. No warning at generation time.

So **0.36.0 is a floor, not a preference**: pinning lower ships an AiKey that is
either absent or wrong. If the pin is ever lowered, `profile` must come out of
`scripts/providers.py` in the same change.

Expect a **one-time CRD regeneration per RestDefinition** on upgrade. It is expected,
it happens once, and it is what applies spec changes previously accepted and dropped.
It runs on the next reconcile rather than at apply time, so on the default 3-minute
poll an operator who applies and immediately inspects the CRD sees the old schema.
That is not a failure.

Earlier floors, kept because they explain why each one moved: 0.22.2 for read-only
identifiers (#75), empty-array drift (#76) and delete verification (#77 plus the #98
regression fix); 0.23.0 because the samples emit dotted identifiers as nested objects
(#109), which 0.22.x refuses as `unknown field "spec.metadata.name"`; 0.27.1 for
envelope unwrapping (#146), jq-shaped request bodies (#148) and credential-header
redaction (#166).

**A release must state the provider version it was validated against.** The CI gate
pins it in one place (`OASGEN_VERSION` in `.github/workflows/validate.yaml`) and the
smoke job re-proves all 35 RestDefinitions against it, so the claim is executed rather
than asserted.
