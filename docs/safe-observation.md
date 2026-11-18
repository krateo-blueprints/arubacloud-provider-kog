---
type: Usage
title: Managing risk with management policies
description: How to point a CR at an existing resource, or restrict a verb, without risking mutation.
tags: [aruba, kog, safety]
timestamp: 2026-09-01T00:00:00Z
---

# Restricting what a controller may do

`unstructured-runtime` — the loop every generated controller runs on — gates
create, update and delete on a **per-CR annotation**. This is the supported way to
adopt an existing resource, run a read-only observation, or protect something
expensive, and it needs no change to the RestDefinition.

## `krateo.io/management-policy`

| Value | Provider may |
|-------|--------------|
| `default` (implicit) | observe, create, update, delete |
| `observe-create-update` | observe, create, update — **never delete** |
| `observe-delete` | observe, delete — **never create or update** |
| `observe` | observe only |

> **Correction (2026-11-18): only the DELETE column is enforced today.** This section said
> "Enforced by `meta.IsActionAllowed`, which reads the annotation and defaults to `default`
> when unset." That function exists and **nothing in `unstructured-runtime` calls it**
> through v1.27.1 — measured across the module cache, with a control: in the same version
> `WasDeleted` has 7 call sites, `IsPaused` 1, `ExternalCreateIncomplete` 1 and
> `GetExternalCreatePending` 1, so the search does find helpers that are consulted.
>
> What IS honoured is `meta.ShouldDelete` (`meta.go:362`), which reads **both** annotations
> and is called from `worker.go:617`. So `management-policy` governs **deletion** today and
> does **not** gate create or update on a managed CR.
>
> The create/update gate arrives in **unstructured-runtime v1.28.0**, which refuses create
> and update under `observe`, `observe-delete` or any unknown value, and reports an absent
> resource under `observe` as `Synced=False` / `ReconcileError` with a
> `CannotObserveExternalResource` event. Until a pin ships that runtime, treat the
> never-create-or-update rows below as **intent, not enforcement**.
>
> **A TYPO IN THIS ANNOTATION REPORTS SUCCESS.** v1.28.0's gate ships without the
> validity condition, so an unrecognised value — `obserev`, `observe_only`, `Observe` —
> is treated as "not permitted to create or update" and produces:
>
> ```
> no create, no update, Synced=TRUE (a no-op success), no event,
> and orphan on CR delete (the finalizer is removed with no handler Delete)
> ```
>
> So a misspelling does not fail loudly and does not fall back to `default`. It produces a
> CR that **affirmatively reports itself synchronised while nothing is written**, which is
> worse than silence: there is a green condition to point at. Until `ManagementPolicyInvalid`
> arrives (later, with the plumbing package), **the only check on this annotation is
> spelling it correctly**, and the table above is the list of values that mean anything.
>
> **Addendum (2026-11-18, later the same day): the v1.28.0 gate is now MEASURED, and the
> way I first tried to measure it was wrong in a way worth recording.**
>
> The gate is real and it is in both v1.28.0 and v1.28.1, implemented as `meta.ShouldCreate`
> and `meta.ShouldUpdate`:
>
> ```
> v1.28.0  pkg/controller/worker.go:923   evt.Create && !meta.ShouldCreate(el)
>                                      || evt.Update && !meta.ShouldUpdate(el)
> v1.28.0  pkg/controller/worker.go:1304  createWithheld := !ResourceExists && !ShouldCreate(el)
> v1.28.0  pkg/controller/worker.go:1305  updateWithheld := ResourceExists && !UpToDate && !ShouldUpdate(el)
> v1.27.1  pkg/controller/worker.go:617   ShouldDelete only -- the pre-gate state
> ```
>
> It landed as krateo-platformops/unstructured-runtime#111, Phase 0 of epic #110, merged
> 17:12:21Z with v1.28.0 published 17:12:34Z — **thirteen seconds later, so there is no
> window in which the fix is merged but unreleased.**
>
> **Do not measure this by counting `IsActionAllowed` call sites.** That function reads
> **0 call sites in v1.27.0, v1.27.1, v1.28.0 AND v1.28.1** — it is now a definition with no
> callers, and its name is exactly what someone reaches for when asking whether the policy is
> enforced. On that evidence I nearly recorded here that the gate was still dead code in
> v1.28.x. A control (`ShouldDelete` = 1 call site) did **not** catch the error, because it
> tested a different function: it proved the search worked while the probe pointed at the
> wrong name. It is marked Deprecated upstream in provider-runtime#111, pointing at the
> `Should*` predicates, and the same point has been relayed for unstructured-runtime's copy.
>
> **ANSWERED, hours later and not by me (2026-11-18).** This paragraph said the runtime
> version behind a given release "cannot be read from the RDC source repositories" and was
> therefore "a question to answer on a cluster". Both halves were wrong, and the answer
> matters because **0.84.0 is now this repository's pin**:
>
> ```
> rest-dynamic-controller:0.84.0  ->  unstructured-runtime v1.27.1
> ```
>
> **So the create/update gate is NOT live at our pin.** Only the delete decision is
> honoured, exactly as the correction above describes for v1.27.1. A reader who sets
> `krateo.io/management-policy: observe` on a managed CR here will still get creates and
> updates.
>
> Measured by core-provider, by pulling the linux/amd64 image, copying the entrypoint
> `/bin/controller` out **without running it**, and reading `go version -m`:
> `dep github.com/krateo-platformops/unstructured-runtime v1.27.1`. The source agrees —
> `go/rest-dynamic-controller/go.mod` at oasgen-provider tag 0.84.0 (`de67f82`) pins
> v1.27.1.
>
> **And the source IS readable; I was looking in dead repositories.** The live source is the
> monorepo `krateo-platformops/oasgen-provider` under `go/rest-dynamic-controller/`. The
> standalone repository of that name has been frozen since the monorepo fold (2026-08-07),
> and the one under the unhyphenated org — which `lint-docs` bans by name, one hyphen from
> the live org — is a separate upstream. My conclusion "neither repository is its source"
> was true of the two I knew about and became a claim about all of them.
>
> **Two boundaries to watch, and they are different per surface:**
>
> | surface | gate live from | measured by |
> |---|---|---|
> | REST resources (this repo's 36 kinds) | an oasgen release whose RDC carries **v1.28.1** — not yet published | pending |
> | compositions (core-provider's cdc) | **core-provider 2.13.115** (2.13.114 carries v1.27.1) | `go version -m` on the cdc image |
>
> `scripts/composition_rollup_probe.sh` pins 2.13.114, so the probe's own runs are on the
> pre-gate runtime.
>
> The unknown-value case is **decision D2** in epic #110 — "invalid acts on nothing, plus a
> named reason" — still open and awaiting the user's decision, so **no release turns a typo
> into an error yet** and the warning above stands.
>
> One further distinction that cost a wrong reading on the way here:
> oasgen-provider's own `restdefinition.go` DOES call `IsActionAllowed` for create, update
> and delete — but on the **RestDefinition** CR, not on the managed resources
> rest-dynamic-controller reconciles. The annotation on a `Vpc` and the annotation on a
> `RestDefinition` were never enforced by the same code.

## `krateo.io/deletion-policy`

| Value | On CR deletion |
|-------|----------------|
| `delete` (implicit) | the external resource is deleted |
| `orphan` | the external resource is left in place |

## Use it for observation runs

Pointing a CR at a resource you did not create is exactly where an accidental `PUT`
is unaffordable. **Both annotations together express that intent. Until the pin ships
unstructured-runtime v1.28.0 or later, only the `orphan` half is enforced** — an earlier
version of this sentence said they "make it structurally impossible", which was false for
every runtime this repository has ever pinned:

```yaml
metadata:
  name: observe-existing-vpc
  annotations:
    krateo.io/management-policy: observe   # cannot create or update
    krateo.io/deletion-policy: orphan      # deleting the CR leaves the VPC alone
```

## Correction: this supersedes the `compareScope` guard

An earlier pass in this repository guarded observation runs by setting
`compareScope: identifiersAndStatus` on the **RestDefinition**. That was the wrong
instrument, for two reasons worth stating plainly:

- **It is cluster-wide.** A RestDefinition is shared by every CR of that kind, so
  narrowing drift to protect one observation CR also silently narrows it for every
  real managed resource of the same kind.
- **It restricts comparison, not action.** It reduces the chance drift is *detected*;
  it does not prevent the controller from acting when drift *is* detected. It is a
  narrower diff, not a safety interlock.

`management-policy` is per-CR and is **intended** to block the action itself; it will do so
from v1.28.0, and today it blocks only deletion. That makes the comparison with
`compareScope` narrower than this section first claimed: `compareScope` restricts
comparison rather than action, which is a real limitation, but calling it "the wrong
instrument" implied the alternative was already a working interlock for create and update.
It was not. `compareScope`
remains the right tool for its actual purpose — deciding which fields constitute
drift for a resource you genuinely do manage.

## Relationship to read-only resources

`management-policy: observe` expresses read-only *intent*, and is the correct way to
say "this cluster does not manage this resource". It does **not** repair
[oasgen-provider#75](https://github.com/krateo-platformops/oasgen-provider/issues/75):
a resource whose API has no create verb still materialises no identifier field, so
there is no way to say *which* instance is meant. The annotation controls what may be
done to a resource; the gap is being unable to name one.
