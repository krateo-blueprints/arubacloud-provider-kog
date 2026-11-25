---
type: Runbook
title: krateo-arubacloud-provider-kog — chart migration to the repository standard
description: How the shipped Composition chart moves to chart/ and is renamed after the repository (REPOSITORY.md R1-R3), what is a code change and what is a per-cluster change planned with the installer's owner, and the two CI gates that pass silently if the move is done wrong.
resource: oci://ghcr.io/krateo-blueprints/charts/aruba-network-environment
tags: [release, chart, standard, migration, installer]
timestamp: 2026-11-25T00:00:00Z
---

# Chart migration to the repository standard

`krateo-blueprints/charts`' `REPOSITORY.md` says where a provider repository's chart
lives and what it is called. This repository satisfies most of that file and deviates
on three rules. **One of the three cannot be fixed by a pull request**: it renames a
chart that is published and installed, and the standard's own
[Changing a published name](https://github.com/krateo-blueprints/charts/blob/main/REPOSITORY.md#changing-a-published-name)
section says that is planned per cluster with the installer's owner, not merged.

This document is the plan. It exists so the cheap and reversible parts can land now
and the expensive one can be decided deliberately, rather than all three arriving as
one pull request that nobody can safely approve.

Everything below was read out of the standard at `krateo-blueprints/charts@main` and out
of `krateo-platformops/.github` at the SHA this repository pins
(`aa90d48e1a101d5e9b3e547bca116b97c49513b5`), on 2026-11-25. Line numbers are that
SHA's.

## Where this repository stands

| Rule | What it requires | Here | |
|---|---|---|---|
| R1 | `<vendor>-provider-kog`, and **no `krateo-` prefix** | `krateo-arubacloud-provider-kog` | **deviates** |
| R2 | one chart, at `chart/`; a config chart at `config-chart/`; no other chart | the chart is at `compositions/aruba-network-environment/` | **deviates** |
| R2 | a never-shipped chart lives under a test/example directory name | `tests/compositions/no-create-rollup/` and `examples/network-environment/` | satisfied |
| R3 | the chart is named after the repository | chart name is `aruba-network-environment` | **deviates** |
| R3 | `home` **and** `sources` name the repository | `sources` does; there is **no `home` key** | **deviates** |
| R4 | a provider chart's `version` and `appVersion` are both `CHART_VERSION` | `version: CHART_VERSION`, `appVersion: "1.0.0"` hand-maintained | see [appVersion](#appversion) |
| R5 | the release is the shared `release-oci` with an explicit `index_channel` | PR #128, in review | in progress |
| R6 | `LICENSE`, `README.md`, `.github/dependabot.yml` | all three present | satisfied |
| R7 | the nine-file docs bundle | all nine present | satisfied |
| R8 | `pinned-uses`, `lint-docs`, `security.yml`, `release-notes`, a reference check | first four call the shared reusables; the reference check is advisory and deliberately absent (see `.github/workflows/security.yml`) | satisfied |

The two test charts are safe by directory name, not by luck: `release-oci` prunes
`examples example __fixtures__ fixtures testdata test tests e2e sample samples docs`
from chart discovery (`release-oci.yaml:476`). A probe chart moved out of one of those
directories would be discovered and published.

## What makes this more than `git mv`

### The chart path is in generated output and in two gates that pass empty

`compositions/` is hard-coded in three places, and two of them fail **silently** when
the path stops matching:

| Where | What it does | On a moved chart |
|---|---|---|
| `scripts/gen_composition.py:41` | `CHART = .../compositions/aruba-network-environment` — the destination of the generated `templates/_generated.tpl` | the next generator run **re-creates the old path**, and a hand-moved tree is reverted. This repository has been bitten by exactly this before; generated paths are not editable by hand |
| `scripts/validate.py:1789` | `glob.glob("compositions/**/Chart.yaml")`, then `if not chart_dirs: return 0` | helm lint and helm template stop running **and the validator still exits 0** |
| `.github/workflows/validate.yaml:335` | `for chart in compositions/*/` guarded by `[ -f "${chart}Chart.yaml" ] || continue` | the glob matches nothing, so (no `nullglob`) the loop iterates **once** over the literal string `compositions/*/`, the guard `continue`s it, `fail` stays `0`, and the job is **green**. Measured, not inferred: `iterations=1 fail=0` |

That third one is the gate that exists because a broken chart shipped twice: it applies
the rendered chart server-side against the CRDs the same job generated, and its own
header records the four defects in published 0.2.0 that `helm lint` and `helm template`
both accepted. Losing it quietly is the worst outcome of this migration, and it is
reached by moving a directory and nothing else.

**So the move is not trusted on a green build.** Green is the failure mode here, which
means the control cannot be "did CI pass" — it has to be "did the gate speak". In this
order:

1. **Move the chart, change nothing else, and expect silence rather than red.**
   `validate_charts` returns `0` having found no chart; the CI loop iterates once and
   `continue`s. Confirm by the **absence** of the per-chart `accepted by the generated
   CRDs` line, because nothing else distinguishes this from a pass. (`gen_composition.py
   --check` will fail loudly in the same run, its target file having moved — that is a
   different gate, and its noise must not be read as the chart gates reporting.)
2. **Update the three paths and re-run.** Both must be loud again: the CI job prints the
   per-chart `accepted by the generated CRDs` line, and `gen_composition.py --check`
   agrees with the tree at the new path, which is what proves the constant moved with
   the files.
3. **Prove the gate still bites at the new path.** Temporarily reintroduce one of the
   defects its header records from published 0.2.0 — an `apiVersion` the generated CRDs
   do not serve is the cheapest — and confirm the job **fails**. Revert it. Without this
   step, steps 1 and 2 are consistent with a gate that moved and with a gate that is
   merely quiet in a new place.

### The CompositionDefinition moves *inside* `chart/`, not to the repository root

Today the CD is at `compositions/compositiondefinition.yaml` — the chart directory's
**parent**. That is one of the two places `release-oci` looks: `ship-cd.sh:46-49` takes
`<chart dir>/compositiondefinition.yaml`, else the one in the chart directory's parent.

After the move, the chart's parent is the **repository root**. A root-level
`compositiondefinition.yaml` is therefore shared by every chart whose parent is the
root — `chart/` today and `config-chart/` the day one exists — and `verify` would refuse
`config-chart` because the CD it inherited installs the *other* chart's URL. The CD
belongs at `chart/compositiondefinition.yaml`, and every future chart carries its own.

### The package cannot be pushed with a half-done rename

`ship-cd.sh verify` reads the chart's name out of the packaged `Chart.yaml` and then
requires the CD inside the package to install exactly `<oci_repo>/<name>` at exactly the
package's version (`:115-121`). So the chart's new name and the CD's `spec.chart.url`
must change in the same commit; a mismatch is refused before anything is pushed rather
than published wrong. This is a genuine safety property of the rename — it is the
*cluster* side, below, that has no such guard.

### A rename is visible to consumers in three more places

- **The published OCI name.** `aruba-network-environment` has 0.2.0, 0.3.0 and 0.4.0
  published at `oci://ghcr.io/krateo-blueprints/charts/aruba-network-environment`.
  Versions already published keep the old address and are never republished
  (PUBLISHING.md rule 6).
- **The release asset's filename.** The asset is named for the chart, so it becomes
  `compositiondefinition-<new name>.yaml`. Anyone downloading it by name breaks. (It
  already changes once in #128, from `compositiondefinition-compositions.yaml`.)
- **`examples/network-environment/compositiondefinition.yaml`**, which users copy and
  apply. It pins the old OCI URL, and a user who applied it has a live CD under the old
  name regardless of what this repository does next.

49 lines across 20 files name `aruba-network-environment` and change in a rename; 10 of
those files are docs. Measured on `main` at `e806f44`; one of the 20 is
`.github/workflows/release-tag.yaml`, which #128 deletes.

## The decision that should be made before the rename, not during it

R3 names the chart after the repository. The chart this repository ships is a
**Composition** that provisions Vpc + Subnet + SecurityGroup. The repository's actual
product is 36 RestDefinitions, which ship as YAML applied with
`kubectl apply -R -f restdefinitions/` and are **not a chart at all**.

So R3, applied literally, makes
`oci://ghcr.io/krateo-blueprints/charts/arubacloud-provider-kog` install a
network-environment composition. Two consequences worth a decision:

1. A consumer who installs the chart named after the provider gets three network
   resources, not the provider.
2. If the RestDefinitions are ever charted, the obvious name is taken — and two
   independent charts make this a **blueprint** rather than a provider under R2, which
   is a different standard again (and one that is still being written).

There is a one-way door here: the rename is the expensive, per-cluster step, and doing
it twice costs twice. **This is Diego's call, and the recommendation is to settle it
before step 3 below, not to discover it afterwards.** The standard's own escape hatch
applies if the answer is that R3 does not fit: *"A rule that does not fit is changed
here, for every repository"* — not exempted in this repository's workflow.

## The plan

Three changes, smallest and most reversible first. Steps 1 and 2 are pull requests.
Step 3 is not.

### Step 1 — R2 only: move the chart, keep its name

`compositions/aruba-network-environment/` → `chart/`, and
`compositions/compositiondefinition.yaml` → `chart/compositiondefinition.yaml`.

Changed: `gen_composition.py`'s `CHART`, `validate.py`'s glob, `validate.yaml`'s loop,
the `compositions/` line in `docs/index.md`'s layout block, and the path references in
docs. **The chart's `name` is untouched**, so the published name, the CD's URL, the
release asset's name and every installed CompositionDefinition are untouched. Nothing
outside this repository can observe step 1.

Gated on the three-part negative control above. No tag is cut as part of it.

### Step 2 — R3 metadata, name still unchanged

Add `home: https://github.com/krateo-blueprints/krateo-arubacloud-provider-kog`.
Settle [appVersion](#appversion). Still no published-name change, still a pull request.

### Step 3 — R1 + R3: the rename, per cluster, with the installer's owner

R1 and R3 land together: R3 derives the chart's name, `home` and `sources` from the
repository's name, so renaming either alone fails `publish-chart`, which compares the
name as a **string** — a GitHub redirect does not satisfy it.

In this repository, in one pull request merged immediately after the GitHub rename, with
**no tag between the two**:

- repository `krateo-arubacloud-provider-kog` → `arubacloud-provider-kog`
- chart `name`, `home`, `sources`, and the `krateo.io/source-repo` annotation where the
  chart carries one
- the CD's `metadata.name` and `spec.chart.url`
- `examples/`, `docs/`, and the generator's and validator's constants

On every cluster, planned with the installer's owner **before** this repository is
touched:

- the new name added and the old one retired in **one** installer change
- the old values key kept until no live installation carries it
- the installed custom resources **proven to survive** the move first

A live CompositionDefinition's removal can take the generated CRDs and every custom
resource with them. So "proven to survive" is a measurement on a cluster, taken before
any cluster that matters is touched, and the bar is:

- `metadata.uid` unchanged — a recreated object is not a surviving object
- `metadata.generation` and `metadata.creationTimestamp` unchanged
- the served CRD schema unchanged
- the children in `status.managed` still listed

`metadata.resourceVersion` is **not** on that list: it changes on any write, including
ones that preserve the object, so reading it as a survival signal produces a false
negative every time.

## appVersion

The chart carries `version: CHART_VERSION` and `appVersion: "1.0.0"`. Only the
placeholder is stamped, so `appVersion` is hand-maintained — and R4 records what that
costs: a chart was found at `0.1.9` while its repository released `0.15.0`. This
repository is at 0.4.0 with `appVersion: "1.0.0"`, which is the same drift, already
happening.

R4's rule is written for a provider chart (one with no CompositionDefinition), where
both fields are `CHART_VERSION`. This chart has a CompositionDefinition, so R4 does not
decide it. Two defensible answers: make `appVersion` the placeholder too, so it tracks
the release and cannot drift; or give it a meaning and maintain it. Recommendation: the
placeholder, because nothing here has a version independent of the repository's — the
chart ships no image, and oasgen-provider spawns the controller.

Either way it is a one-line change that belongs in step 2, where it is observable in a
dry run before a tag.

## What the installer's owner is being asked

Not an approval of this document — two readings this repository cannot take:

1. Does any Installer CR, `component-pins.yaml` entry or `componentValues` key name
   `aruba-network-environment`, on any cluster? If it does, step 3 has a blast radius
   this document has not costed.
2. What does "the new name added and the old one retired in one installer change" look
   like concretely — one commit, or a pin plus a values change that cannot be atomic?
   If it cannot be atomic, step 3 needs a window, and the plan needs to say which half
   lands first.
