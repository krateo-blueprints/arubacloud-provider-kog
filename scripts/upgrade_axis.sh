#!/usr/bin/env bash
# Measure the IN-PLACE UPGRADE axis: a stack installed at one oasgen-provider version,
# upgraded to another, with the CRs created under the first still in place.
#
# Usage:
#   KUBECONFIG=<kubeconfig of a cluster already installed at FROM> \
#   FROM=0.67.0 TO=0.84.0 PROJECT=<aruba project id> \
#     scripts/upgrade_axis.sh [CHAIN]
#
#   CHAIN defaults to tests/ga/chains/network-security.yaml (free: VPCs do not bill).
#
# WHY THIS AXIS NEEDS ITS OWN SCRIPT. scripts/pin_verify.sh measures a FRESH install of
# one version: cluster, stack, chain, teardown. Neither it nor the offline shape axis can
# see what happens to objects that were STORED under an older schema and are now
# reconciled by newer controllers, and that is the path a real operator takes. The three
# axes answer different questions and this is the third.
#
# THE INSTRUMENT, and why it is not a CRD diff. docs/oasgen-provider-evolution.md
# retracted "upgrade, then diff the CRDs": that diff cannot distinguish "nothing changed"
# from "nothing was re-rendered", because both look identical. The discriminator is
# `status.generatorBuild`, which oasgen-provider's restdefinition.go compares as its FIFTH
# drift trigger -- the generator is an input that nothing else hashes. If the build moves,
# a re-render demonstrably happened; if it does NOT move while the provider version did,
# the CRD was left on the old generator while the RestDefinition kept reporting Ready,
# which is oasgen-provider#453.
#
# WHAT IS DELIBERATELY NOT DONE HERE
#   * no `helm upgrade --install`. The release MUST already exist: --install would
#     silently create a fresh one and the run would measure a fresh install while
#     reporting an upgrade.
#   * no re-apply of the RestDefinitions or the ConfigMaps. The upgrade under test is the
#     PROVIDER's; re-applying its inputs would change what the provider sees and the
#     generatorBuild assertion would no longer isolate trigger 5.
#   * no cluster creation and no cluster deletion. This script measures a cluster someone
#     else's run left alive, and leaving it alive is the caller's decision to undo.
set -euo pipefail

CHAIN="${1:-tests/ga/chains/network-security.yaml}"
NS="${NS:-krateo-system}"
: "${FROM:?set FROM to the version the cluster is installed at, e.g. FROM=0.67.0}"
: "${TO:?set TO to the version to upgrade to, e.g. TO=0.84.0}"
: "${KUBECONFIG:?set KUBECONFIG to the cluster already installed at FROM}"
: "${PROJECT:?set PROJECT: the chain needs it, and discovering that AFTER the upgrade
wastes the measurement -- this is the #98 lesson, refusing before anything moves}"

W="${WORKDIR:-${TMPDIR:-/tmp}}"
BEFORE="${W}/upgrade-axis-before.json"
AFTER="${W}/upgrade-axis-after.json"
say() { printf '\n=== %s\n' "$*"; }
die() { printf '\nupgrade_axis: %s\n' "$*" >&2; exit 1; }

[ -f "$CHAIN" ] || die "chain not found: $CHAIN"

say "PREFLIGHT -- refusing before anything moves, not after"

ctx="$(kubectl config current-context)" || die "no current context"
# EXPORTED, because scripts/ga_chain.py reads KUBE_CONTEXT and DEFAULTS IT TO A KIND
# CONTEXT. Left unset, every kubectl call in the chain goes to `kind-aruba-ga`, which on a
# GKE cluster does not exist -- and the chain then reports "no token in
# default/arubacloud-token", a message about the credential for a failure about the
# cluster. That cost the behavioural half of a measurement whose upgrade had already been
# spent, so it is set here rather than left to the caller.
export KUBE_CONTEXT="$ctx"
printf '    context          %s\n' "$ctx"
printf '    from -> to       %s -> %s\n' "$FROM" "$TO"
printf '    chain            %s\n' "$CHAIN"

# Every RestDefinition must currently carry FROM. If one already carries TO, this cluster
# has been upgraded and a "generatorBuild moved" verdict would be measuring nothing.
builds="$(kubectl get restdefinitions.ogen.krateo.io -A \
            -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.status.generatorBuild}{"\n"}{end}')" \
  || die "could not read RestDefinitions"
[ -n "$builds" ] || die "no RestDefinitions on this cluster: nothing to upgrade"
printf '%s\n' "$builds" | sed 's/^/    /'
bad="$(printf '%s\n' "$builds" | grep -v "=${FROM}-" || true)"
if [ -n "$bad" ]; then
  die "these RestDefinitions do not carry generatorBuild ${FROM}-*, so this cluster is
not at FROM and the before-reading would be meaningless:
${bad}"
fi

# And there must be CRs to measure. The whole axis is about objects that already exist.
crs="$(kubectl get vpcs,subnets,securitygroups,securityrules -A --no-headers 2>/dev/null | wc -l | tr -d ' ')"
[ "${crs:-0}" -gt 0 ] || die "no managed CRs on this cluster. The axis measures CRs an
earlier run left behind (run the chain with --keep first); with none, an upgrade here
would measure nothing and report it as a pass."
printf '    CRs to carry     %s\n' "$crs"

# THE CHAIN MUST BE ABLE TO READ ITS TOKEN, proven here rather than discovered after the
# upgrade. Same class as the PROJECT check: everything the second half needs is verified
# while the first half can still be called off. Read through the SAME context and the same
# Secret coordinates the chain uses, because checking it any other way checks something
# else. The value is never printed -- only whether it is there.
tok_ns="${ARUBA_TOKEN_SECRET_NS:-default}"
tok_name="${ARUBA_TOKEN_SECRET:-arubacloud-token}"
# `|| true` INSIDE the substitution on purpose. With `set -o pipefail` a failing kubectl
# makes the whole pipeline non-zero, `set -e` then kills the script AT THE ASSIGNMENT, and
# the operator gets exit 1 with no message at all -- which is how this guard behaved when
# first written: it stopped the run for the right reason while explaining nothing.
tok_len="$( { kubectl --context "$ctx" get secret "$tok_name" -n "$tok_ns" \
               -o jsonpath='{.data.token}' 2>/dev/null | wc -c | tr -d ' '; } || true )"
[ "${tok_len:-0}" -gt 1 ] || die "the chain's token is not readable at
${tok_ns}/${tok_name} through context '${ctx}'. The behavioural half of this axis would
fail AFTER the upgrade had been spent, so it is refused now. If ESO has not produced it
yet, wait; if it lives elsewhere, set ARUBA_TOKEN_SECRET_NS / ARUBA_TOKEN_SECRET."
printf '    token            readable at %s/%s (%s bytes)\n' "$tok_ns" "$tok_name" "$tok_len"

say "BEFORE snapshot"
python3 -I scripts/upgrade_snapshot.py --take "$BEFORE" || die "before-snapshot failed"

say "upgrading the stack in place: ${FROM} -> ${TO}"
# upgrade, NOT upgrade --install: see the header.
helm upgrade oasgen-provider-crds \
  "oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds" --version "$TO" \
  --namespace "$NS" --wait || die "crds chart upgrade failed"
helm upgrade oasgen-provider \
  "oci://ghcr.io/krateo-platformops/charts/oasgen-provider" --version "$TO" \
  --namespace "$NS" --wait --timeout 5m || die "provider chart upgrade failed"

say "images actually running after the upgrade"
kubectl get deploy -n "$NS" \
  -o jsonpath='{range .items[*]}    {.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'

say "waiting for the re-render: every RestDefinition's generatorBuild must reach ${TO}"
# A TIMEOUT HERE IS A RESULT, NOT AN ERROR. If the build never moves, that is #453's
# shape and the snapshot must still be taken and reported -- so this loop reports what it
# saw and lets the diff deliver the verdict, rather than dying and leaving no record.
deadline=$(( $(date +%s) + ${RENDER_TIMEOUT:-300} ))
while :; do
  left="$(kubectl get restdefinitions.ogen.krateo.io -A \
            -o jsonpath='{range .items[*]}{.metadata.name}{"="}{.status.generatorBuild}{"\n"}{end}' \
          | grep -v "=${TO}-" || true)"
  [ -n "$left" ] || { echo "    every RestDefinition carries ${TO}"; break; }
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "    TIMED OUT after ${RENDER_TIMEOUT:-300}s; these still do not carry ${TO}:"
    printf '%s\n' "$left" | sed 's/^/      /'
    echo "    continuing: the diff reports this as the finding it is"
    break
  fi
  sleep 10
done

say "AFTER snapshot"
python3 -I scripts/upgrade_snapshot.py --take "$AFTER" || die "after-snapshot failed"

say "DIFF"
diff_rc=0
python3 -I scripts/upgrade_snapshot.py --diff "$BEFORE" "$AFTER" --to "$TO" || diff_rc=$?

say "observing the PRE-EXISTING CRs through the upgraded controllers"
# --existing, so the chain observes what is already there instead of creating anything;
# it then drifts and deletes those same CRs, which is the half a snapshot cannot show.
chain_rc=0
PROJECT="$PROJECT" python3 -u scripts/ga_chain.py "$CHAIN" --existing || chain_rc=$?

say "VERDICT"
printf '    snapshot diff   exit %s\n' "$diff_rc"
printf '    chain --existing exit %s\n' "$chain_rc"
printf '    before          %s\n' "$BEFORE"
printf '    after           %s\n' "$AFTER"
if [ "$diff_rc" -eq 0 ] && [ "$chain_rc" -eq 0 ]; then
  echo "    IN-PLACE UPGRADE ${FROM} -> ${TO} PASSES: the stored CRs survived the"
  echo "    re-render, still observe, still correct drift, and still delete upstream."
  exit 0
fi
echo "    IN-PLACE UPGRADE ${FROM} -> ${TO} DID NOT PASS -- read both results above."
echo "    The cluster is left alive deliberately so the failure can be inspected."
exit 1
