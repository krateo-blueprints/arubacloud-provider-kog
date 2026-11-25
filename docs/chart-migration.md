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
| R2 | one chart, at `chart/`; a config chart at `config-chart/`; no other chart | the chart is at `chart/` | **satisfied, 2026-11-25** |
| R2 | a never-shipped chart lives under a test/example directory name | `tests/compositions/no-create-rollup/` and `examples/network-environment/` | satisfied |
| R3 | a `chart/` that carries a `compositiondefinition.yaml` is named after what it provisions, and carries `krateo.io/source-repo` | named `aruba-network-environment`, and the annotation is present | **satisfied, 2026-11-25** |
| R3 | `home` **and** `sources` name the repository | both name `krateo-blueprints/krateo-arubacloud-provider-kog` | **satisfied, 2026-11-25** |
| R4 | a provider chart's `version` and `appVersion` are both `CHART_VERSION` | both are `CHART_VERSION` | **satisfied, 2026-11-25** — see [appVersion](#appversion) |
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
| `scripts/gen_composition.py:41` | `CHART = .../compositions/aruba-network-environment` — the destination of the generated `templates/_generated.tpl` | the next generator run **re-creates the old path**, and a hand-moved tree is reverted. This repository has been bitten by exactly this before; generated paths are not editable by hand. Note it is `os.path.join(ROOT, "compositions", …)`, so **a grep for `compositions/` does not find this line** |
| `scripts/validate.py:1789` | `glob.glob("compositions/**/Chart.yaml")`, then `if not chart_dirs: return 0` | helm lint and helm template stop running **and the validator still exits 0**, reporting `0 chart(s)` in a summary line |
| `.github/workflows/validate.yaml:335` | `for chart in compositions/*/` guarded by `[ -f "${chart}Chart.yaml" ] || continue` | the glob matches nothing, so (no `nullglob`) the loop iterates **once** over the literal string `compositions/*/`, the guard `continue`s it, `fail` stays `0`, and the job is **green**. Measured, not inferred: `iterations=1 fail=0` |
| `docs/llms.txt` | lists repository files by path; the shared `lint-docs` reusable checks each entry exists | **red**, on every moved entry. A third gate, and a loud one — it was not in the first draft of this plan |

That third one is the gate that exists because a broken chart shipped twice: it applies
the rendered chart server-side against the CRDs the same job generated, and its own
header records the four defects in published 0.2.0 that `helm lint` and `helm template`
both accepted. Losing it quietly is the worst outcome of this migration, and it is
reached by moving a directory and nothing else.

**So the move is not trusted on a green build.** Green is the failure mode here, which
means the control cannot be "did CI pass" — it has to be "did the gate speak".

### What the control actually measured

This section first predicted that a bare move would go green-and-silent. **It does not,
and the correction matters more than the prediction did**, because it changes which
version of this move is dangerous. Measured on PR #130:

| Commit | What was moved | Result |
|---|---|---|
| `29b5ee4` | the directory, and **nothing else** | **caught loudly, twice.** `static validation` red — `STALE: compositions/aruba-network-environment/templates/_generated.tpl differs from what gen_composition.py would write`. `lint-docs` red on three `docs/llms.txt` entries that no longer exist. And the chart gate **never ran at all**: the smoke job is `needs: validate` (`validate.yaml:181`), so a red generator gate stops it |
| `59e1a98` | the directory, the generator constant and the docs — the two chart gates left pointing at `compositions/` | **a fully green build that checked no chart.** `static validation` **passed**, printing `Checked 36 RestDefinitions, 12 ConfigMaps, 0 chart(s).` `kind smoke test` **succeeded**, and its per-chart `accepted by the generated CRDs` string appears in the log *only as the echo statement's source, never as output*. `lint-docs` passed |

So the hazard is not "move the directory". A bare move is caught. **The dangerous version
is the careful-looking one**: fix everything CI complained about, stop there, and the two
chart gates are disarmed while every check is green — including the server-side-apply
gate that exists because a broken chart shipped twice. "I fixed what CI reported" is
precisely the state in which this fails.

### The control, as it should be run

1. **Move the chart alone.** Expect red, from the generator and from `lint-docs`. This
   proves those two follow the chart; it proves nothing about the chart gates, which do
   not even run.
2. **Satisfy the loud gates and stop.** Expect green — and read the log, not the verdict:
   `0 chart(s)` and a missing per-chart line are the hazard, visible only there.
3. **Fix the chart gates and re-run.** The count must be non-zero and the per-chart
   `accepted by the generated CRDs` line must be **printed**.
4. **Prove the gate still bites at the new path.** Reintroduce one of the defects its
   header records from published 0.2.0 — an `apiVersion` the generated CRDs do not serve
   is the cheapest — confirm the job **fails**, and revert. Without this, step 3 is
   consistent with a gate that moved and with a gate that is merely quiet somewhere new.

The repair is not new paths but the end of discovery: `chart/` and `config-chart/` are
named explicitly, since R2 fixes that set, and an empty set is an **error** rather than a
`return 0`. A glob answers "what is there"; what a gate needs to know is "is what must be
there, there".

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

### What a chart rename would have been visible in — kept as the cost that was avoided

**None of this happens any more**, because the R3 amendment below means the chart keeps
its name. It is recorded because it is what the amendment bought, and because it is the
checklist for any future change that does rename a published chart.

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

49 lines across 20 files name `aruba-network-environment` and would have changed in a
rename; 10 of those files are docs. Measured on `main` at `e806f44`; one of the 20 is
`.github/workflows/release-tag.yaml`, which #128 deletes. Under the amendment, **none of
the 49 changes** — the R1 rename touches only the lines that name the *repository*.

## The amendment that removed step 3

**This section asked for a decision, Diego made it, and the rule changed — so the
expensive step is gone.** What follows is kept because the reasoning is what produced the
amendment, and because anyone reading this plan later should see why the chart is not
named after the repository.

R3 is amended in `krateo-blueprints/charts#58`: a `chart/` that carries a
`compositiondefinition.yaml` is named after **what it provisions**, and must carry a
`krateo.io/source-repo` annotation naming its repository; `home` and `sources` are
unchanged. The ownership guarantee R3 exists to provide never came from the name —
`publish-chart` refuses a chart whose `sources` does not list its publishing repository,
comparing a string — so naming by content costs nothing and traceability moves to the
annotation.

What that buys, concretely: **`aruba-network-environment` keeps its name, so the published
OCI name never changes, so there is no published-name rename at all.** No per-cluster
CompositionDefinition retirement, no CRD-and-custom-resource survival proof, nothing for
the installer's owner to sequence. The whole of
[Changing a published name](https://github.com/krateo-blueprints/charts/blob/main/REPOSITORY.md#changing-a-published-name)
stops applying to this repository.

The question acmp asked before taking it — whether a consumer's pin loses "find the
source" — is answered in the issue from `krateo-platformops/installer` on `main`: a pin is
chart name + version against an `ociRepo` base, **no GitHub repository appears in a pin at
all**, and `pin-version-lines.yaml`'s `oasgen` group already pins three chart names from
one source repository, `controller-render-service` among them. The case the amendment
creates already ships.

## The reasoning that produced it

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

### Step 1 — R2 only: move the chart, keep its name — **DONE, released in 0.5.0**

Merged as #130 on 2026-11-25 and released in **0.5.0**. The prediction below that a bare
`git mv` goes green-and-silent was **wrong and was corrected in place before the move**: it
goes RED, on a stale generated file and three dead `docs/llms.txt` entries. The hazard is
the *careful-looking* move, not the careless one.

Confirmed by the shared release itself rather than by this repository's own CI, which is
the only check that could have caught a discovery mismatch:

```
First-class charts: ./chart
./chart: ships ./chart/compositiondefinition.yaml, stamped 0.5.0
Successfully packaged chart and saved it to: dist/aruba-network-environment-0.5.0.tgz
aruba-network-environment-0.5.0.tgz: carries aruba-network-environment/compositiondefinition.yaml
  installing oci://ghcr.io/krateo-blueprints/charts/aruba-network-environment 0.5.0
```

Exactly one chart discovered, `tests/` and `examples/` pruned as designed, and the
published **name unchanged** — which is what the R3 amendment was for, and why no cluster
had to be touched. One consequence not foreseen here: the release **asset** is named after
the chart, so the move renamed it from `compositiondefinition-compositions.yaml` to
`compositiondefinition-aruba-network-environment.yaml`. See `docs/release.md`.

The original plan, kept because the reasoning is the point:

`compositions/aruba-network-environment/` → `chart/`, and
`compositions/compositiondefinition.yaml` → `chart/compositiondefinition.yaml`.

Changed: `gen_composition.py`'s `CHART`, `validate.py`'s glob, `validate.yaml`'s loop,
the `compositions/` line in `docs/index.md`'s layout block, and the path references in
docs. **The chart's `name` is untouched**, so the published name, the CD's URL, the
release asset's name and every installed CompositionDefinition are untouched. Nothing
outside this repository can observe step 1.

Gated on the three-part negative control above. No tag is cut as part of it.

### Step 2 — R3 metadata, name still unchanged — **DONE 2026-11-25**

Merged after 0.5.0, so it ships in the next release rather than retroactively. `sources` was
already correct; this added `home` and the `krateo.io/source-repo` annotation, both naming
**today's** repository name, R1 deviation included — a field naming a repository that does not
exist yet is worse than one naming the repository that does. All three strings move together in
one pull request if R1 ever lands.

`appVersion` was settled in the same change, as planned below: it is now `CHART_VERSION` too.
Two things were verified first, and the second is the interesting one.

**Nothing consumes it.** `.Chart.AppVersion` appears nowhere in `chart/`. Four docs
(`README.md`, `docs/usage.md`, `docs/getting-started.md`, `docs/troubleshooting.md`) say "the
chart derives `rdc.image.tag` from its own `appVersion`" and **none of them means this chart** —
that is the oasgen-provider chart. Had it meant this one, stamping `appVersion` from the tag
would have changed which controller image gets deployed.

**`scripts/validate.py`'s substitution was anchored, and its comment said it was not.** It read

```python
re.sub(r"^version:\s*CHART_VERSION\s*$", "version: 0.0.0", txt, flags=re.M)
```

with a comment claiming it did "exactly what the release does". The release does
`sed "s/CHART_VERSION/${CHART_VERSION}/g"` — globally. Measured on this chart:

```
OLD anchored   version='0.0.0'  appVersion='CHART_VERSION'   helm lint: PASSED (rc=0)
NEW global     version='0.0.0'  appVersion='0.0.0'           helm lint: PASSED (rc=0)
```

**helm lint passes on the literal placeholder**, because `version` must be SemVer and
`appVersion` is a free string. So the anchored form would have validated a chart no release
could ever produce, silently. This is R4's named hazard, which predicted it exactly: *"a
substitution anchored to `version:` silently leaves `appVersion` as the literal placeholder,
and helm accepts that without a word."* Fixed to a global replace in the same change.

Making `appVersion` a placeholder also activates a release gate that was dormant: release-oci
only verifies the packaged `appVersion` against the release version when the field is authored
as a placeholder. With a literal it never checked.

The original plan:

Add to `chart/Chart.yaml`:

```yaml
home: https://github.com/krateo-blueprints/krateo-arubacloud-provider-kog
annotations:
  krateo.io/source-repo: https://github.com/krateo-blueprints/krateo-arubacloud-provider-kog
```

**The annotation names the repository as it is TODAY, not as R1 will rename it.** A
pre-emptive post-rename URL names a repository that does not exist, and the standard
already handles the transition the other way: in a repository rename, *"every place that
names the repository (`home`, `sources`, and the `krateo.io/source-repo` annotation where
the chart carries one) changes in one pull request"*. So the annotation joins that set
rather than anticipating it.

Settle [appVersion](#appversion) in the same change. Still no published-name change,
still a pull request.

### Step 3 — R1: rename the repository. No longer a per-cluster change.

**This was the expensive step. The R3 amendment removed its cost**, because the chart's
name no longer derives from the repository's: `aruba-network-environment` stays, so the
published OCI name stays, so nothing installed anywhere is renamed or retired.

R1 still applies — `krateo-arubacloud-provider-kog` carries the `krateo-` prefix R1
forbids, and should be `arubacloud-provider-kog`. What that now requires, in one pull
request merged immediately after the GitHub rename with **no tag between the two**:

- `home`, `sources`, and the `krateo.io/source-repo` annotation from step 2
- the repository URL wherever docs and examples name it

And what it no longer requires, all of it deleted by the amendment:

- ~~the chart's `name`~~ — unchanged
- ~~the CompositionDefinition's `metadata.name` and `spec.chart.url`~~ — both name the
  chart, not the repository
- ~~the release asset's filename~~ — named for the chart
- ~~per-cluster planning with the installer's owner~~ — no published name changes, so no
  live CompositionDefinition is retired and no CRD or custom resource is at risk
- ~~a survival proof before any cluster is touched~~ — nothing is removed to survive

The one thing that still bites if done carelessly: `publish-chart` compares `sources` as
a **string**, and a GitHub redirect does not satisfy it. So a tag cut between the GitHub
rename and the merge of that pull request publishes nothing — hence "no tag between the
two", which is the whole of the remaining risk.

The survival bar that step 3 used to need is kept below, because it is the right bar for
any future change that does remove a live CompositionDefinition — just not for this one.

#### The survival bar, for whenever it is next needed

A live CompositionDefinition's removal can take the generated CRDs and every custom
resource with them. "Proven to survive" is a measurement on a cluster, taken before any
cluster that matters is touched:

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

## What the installer's owner was asked, and why it no longer blocks

Two readings this repository could not take were put to the installer's owner, because
step 3 as originally planned had a per-cluster blast radius this document had not costed:

1. Does any Installer CR, `component-pins.yaml` entry or `componentValues` key name
   `aruba-network-environment`, on any cluster?
2. Can "the new name added and the old one retired" be **one** installer change, or does
   it need a window — and if so, which half lands first?

**The R3 amendment made both moot for this migration**: no chart is renamed, so no pin
changes, nothing is retired, and there is no window to sequence. Question 1 is still worth
an answer for its own sake — knowing whether a pin names this chart is useful the next
time anything about it changes — but nothing waits on it.

The one thing worth carrying to the installer's owner instead is the gate finding above,
because it is not specific to charts: any gate that **discovers** its targets by glob,
find, label selector or directory listing reports success when it finds none, and a
component that moves takes its gate's coverage with it silently.
