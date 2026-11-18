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
