#!/usr/bin/env bash
# Observe the PARENT condition of a composition containing a child that can never be
# created, and the raw conditions of both children.
#
# WHY THIS EXISTS. docs/oasgen-provider-evolution.md closes oasgen#660 for our two
# no-create kinds with "two separately measured halves": 0.67.0 emits
# NoCreateVerbDeclared, and core-provider >= 2.13.96 (#496) maps that reason to
# childFailed. The same appendix then says, in as many words:
#
#   The composition itself has **not** been observed at 0.67.0; the parent's verdict
#   follows from the two halves rather than from a run.
#
# This script replaces that inference with a measurement. It is the only thing it does.
#
# WHAT IT DOES NOT DO: build a cluster or install the oasgen stack. pin_verify.sh already
# owns cluster lifecycle, the SSD-quota guard, the isolated kubeconfig and the RestDefinition
# scoping, and a second copy of that would rot. Run that first and keep the cluster:
#
#   CLUSTER_PROVIDER=gke SKIP_CHAIN=1 KEEP=1 \
#   CHAIN=tests/compositions/no-create-rollup/controllers.chain.yaml \
#     ./scripts/pin_verify.sh
#   PROJECT=<aruba project id> ./scripts/composition_rollup_probe.sh
#
# This script attaches to the cluster that left behind, and REFUSES rather than building
# one, so a missing stack can never be reported as a composition failure.
#
# THE PIN IS DELIBERATE. It runs at whatever oasgen pin pin_verify installed, and it
# follows the pin rather than naming a version, so this comment does not go stale when the
# pin moves.
#
# WHAT THE PIN MOVE TO 0.84.0 DID TO THE EARLIER RESULT, said here because the result is
# cited elsewhere as though it described the pinned version. The probe ran once, and it ran
# at 0.67.0 with core-provider 2.13.114: parent Ready=False/Unavailable naming
# `managed children not healthy: loadbalancers/krateo-system/probe-lb`, 4 managed / 2
# evaluated / 2 noReadyCondition. THAT MEASUREMENT IS NOW HISTORY, not a statement about
# the pin:
#
#   * oasgen 0.67.0 emitted NoCreateVerbDeclared. Whether 0.84.0's PRODUCER still emits it
#     is oasgen's to confirm and is NOT proved by a reason string declared at head -- the
#     original reasoning here, unchanged and still correct.
#   * THE CONSUMER HALF DID NOT MOVE, and the line that used to stand here saying it did
#     was wrong. I wrote that core-provider 2.13.115 -- the first cdc image embedding
#     unstructured-runtime v1.28.1, where the management-policy gate is live -- had moved
#     this probe's consumer half, because 2.13.114 (pinned here) carries v1.27.1. That
#     reasoning puts the gate in the wrong process. core-provider answered from
#     childhealth.go: THE GATE RUNS IN THE CHILD'S OWN CONTROLLER. This probe's child is
#     `loadbalancers`, a REST kind, so its controller is rest-dynamic-controller -- which on
#     oasgen 0.84.0 is still unstructured-runtime v1.27.1, so the gate is not live for it at
#     all. cdc 2.13.115's gate applies to COMPOSITIONS (including a composition used as a
#     child), not to REST children. So 2.13.115 changes nothing about what this probe
#     measured, and only the producer half above is open.
#
# So a re-run at the 0.84.0 pin is its own exercise with its own cluster, and until it
# happens the 0.67.0 reading must be quoted WITH its version rather than as "the probe
# result".
set -euo pipefail

say()  { printf '\n=== %s ===\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '    %s\n' "$*"; }

# ---------------------------------------------------------------------------- inputs
CLUSTER="${CLUSTER:-aruba-pin}"
CLUSTER_PROVIDER="${CLUSTER_PROVIDER:-gke}"
GKE_PROJECT="${GKE_PROJECT:-operations-dev-krateo-io}"
GKE_ZONE="${GKE_ZONE:-europe-west3-a}"
case "$CLUSTER_PROVIDER" in
  kind) CTX="kind-${CLUSTER}" ;;
  gke)  CTX="gke_${GKE_PROJECT}_${GKE_ZONE}_${CLUSTER}" ;;
  *)    die "unknown CLUSTER_PROVIDER=${CLUSTER_PROVIDER}" ;;
esac
# The SAME isolated kubeconfig pin_verify.sh writes. Exported, because a plain `kubectl`
# here would otherwise read ~/.kube/config, which other sessions on this machine also
# select clusters in -- the shared-kubeconfig hazard. Every call below also passes
# --context explicitly, so neither mechanism alone can put us on someone else's cluster.
PIN_KUBECONFIG="${PIN_KUBECONFIG:-${TMPDIR:-/tmp}/${CLUSTER_PROVIDER}-${CLUSTER}.kubeconfig}"
export KUBECONFIG="$PIN_KUBECONFIG"

NS="${NS:-krateo-system}"
CHART_DIR="${CHART_DIR:-tests/compositions/no-create-rollup}"
CHART_NAME=aruba-no-create-rollup-probe
CORE_PROVIDER_VERSION="${CORE_PROVIDER_VERSION:-2.13.114}"   # the release that raised the
                                                      # chart-version cap 20 -> 64 and ships
                                                      # the crds NOTES.txt (#588, #589).
                                                      # PINNED, not floating: resolving
                                                      # `latest` made the version drift
                                                      # between runs of the same probe.
CDC_FLOOR="${CDC_FLOOR:-2.13.96}"                    # #496 landed here; below it the arm is absent
KEEP="${KEEP:-0}"
PROJECT="${PROJECT:-}"
LOCATION="${LOCATION:-ITBG-Bergamo}"
TOKEN_SECRET_NS="${TOKEN_SECRET_NS:-default}"
TOKEN_SECRET_NAME="${TOKEN_SECRET_NAME:-arubacloud-token}"
# nginx pinned by tag on purpose: an unpinned :alpine changes under you and a chart server
# that silently changes is one more thing that can explain a result.
HTTPD_IMAGE="${HTTPD_IMAGE:-nginx:1.27.3-alpine}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

API="${ARUBA_API:-https://api.arubacloud.com}"
# curl, not busybox wget, and PINNED. curl does not follow redirects unless asked, which
# matters here: Aruba's API ROOT answers with a cross-origin redirect, and a client that
# follows it re-sends the Authorization header to another origin. Every request below names
# an exact collection path and nothing follows a 3xx -- that is the only reason this is safe.
CURL_IMAGE="${CURL_IMAGE:-curlimages/curl:8.11.1}"
[ -d "$CHART_DIR" ] || die "no chart at ${CHART_DIR}"

# ------------------------------------------------------------------ the cluster is ours
say "attaching to ${CTX}"
kubectl --context "$CTX" version -o json >/dev/null 2>&1 \
  || die "cannot reach context ${CTX} via ${PIN_KUBECONFIG}.
Run pin_verify.sh with KEEP=1 first (see the header). Not building a cluster here on
purpose: a stack this script built itself is a stack whose failures it would misreport."
# POSITIVE CONTROL on the stack, not just the cluster. A reachable apiserver with no
# RestDefinition CRDs would let every later step fail in ways that read like cdc defects.
for crd in restdefinitions.ogen.krateo.io loadbalancers.arubacloud.ogen.krateo.io \
           vpcs.arubacloud.ogen.krateo.io; do
  kubectl --context "$CTX" get crd "$crd" >/dev/null 2>&1 \
    || die "CRD ${crd} is absent -- the oasgen stack and the two RestDefinitions are not
installed. This script measures cdc's rollup, and cannot do that on a cluster where the
children's own CRDs do not exist."
done
note "oasgen stack present, both child CRDs served"
kubectl --context "$CTX" get secret -n "$TOKEN_SECRET_NS" "$TOKEN_SECRET_NAME" >/dev/null 2>&1 \
  || die "Secret ${TOKEN_SECRET_NS}/${TOKEN_SECRET_NAME} is absent. pin_verify.sh creates it
through ESO; this script does not create credentials."

# ------------------------------------------------------------------- the project id
# Every call runs INSIDE the cluster with the token injected from the Secret by the
# apiserver. The credential never reaches this machine, this script's argv, or its log --
# only the response body does, and a project listing is metadata, not a credential.
#
# The pod runs in the Secret's namespace because secretKeyRef is namespace-local.
aruba_get() {
  # $1 = path. MUST be an exact collection or object path: never the API root, which
  # answers with a cross-origin redirect. curl is not given -L, so a 3xx is returned as a
  # 3xx and this function reports it rather than re-sending the header somewhere else.
  #
  # A pod manifest rather than `kubectl run --overrides`: the override form needs the same
  # command written twice with two levels of escaping, and two copies of one string is how
  # the wrong copy ends up being the one that runs.
  local path="$1" name="probe-api-${RANDOM}${RANDOM}" out=""
  kubectl --context "$CTX" apply -f - >/dev/null <<YAML || return 1
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${TOKEN_SECRET_NS}
  labels: { app: probe-api }
spec:
  restartPolicy: Never
  containers:
  - name: curl
    image: ${CURL_IMAGE}
    env:
    # Injected by the apiserver from the Secret. The value never reaches this machine,
    # this script's argv, or its log.
    - name: ARUBA_TOKEN
      valueFrom:
        secretKeyRef:
          name: ${TOKEN_SECRET_NAME}
          key: token
    - name: URL
      value: "${API}${path}"
    command: ["sh","-c"]
    args:
    # The status line is printed FIRST and separately, so a body that happens to contain
    # the word HTTP cannot be mistaken for it.
    - |
      code=\$(curl -sS -o /tmp/body -w '%{http_code}' \
                -H "Authorization: Bearer \$ARUBA_TOKEN" "\$URL") || code=000
      printf 'HTTP:%s\\n' "\$code"
      cat /tmp/body 2>/dev/null || true
YAML
  kubectl --context "$CTX" wait --for=jsonpath='{.status.phase}'=Succeeded \
    "pod/${name}" -n "$TOKEN_SECRET_NS" --timeout=120s >/dev/null 2>&1 || true
  out="$(kubectl --context "$CTX" logs "pod/${name}" -n "$TOKEN_SECRET_NS" 2>/dev/null || true)"
  kubectl --context "$CTX" delete "pod/${name}" -n "$TOKEN_SECRET_NS" \
    --wait=false >/dev/null 2>&1 || true
  printf '%s' "$out"
}

if [ -z "$PROJECT" ]; then
  say "resolving the Aruba project id from the account (in-cluster)"
  PROJ_RAW="$(aruba_get "/projects?api-version=1.0" || true)"
  PROJ_CODE="$(printf '%s' "$PROJ_RAW" | sed -n 's/^HTTP:\([0-9][0-9]*\).*/\1/p' | head -1)"
  [ "$PROJ_CODE" = "200" ] || die "listing /projects returned HTTP ${PROJ_CODE:-<none>}.
Not guessing a project id from a failed read -- a wrong id makes the Vpc child fail its
create, and an unhealthy parent for THAT reason looks exactly like the result this probe
is trying to measure."
  # ids parsed with a JSON parser, not a grep: a regex over a response body is how an id
  # from the wrong field ends up in a path.
  PROJ_IDS="$(printf '%s' "$PROJ_RAW" | sed '1d' | python3 -I -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    sys.exit("could not parse the /projects response as JSON: %s" % e)
# Aruba answers {"total": N, "values": [...]}. MEASURED against the live API, not
# guessed: the first version of this looked for value/items/data and failed on the
# plural -- `values`, not `value`. The alternatives stay as fallbacks, and the error
# below names every key tried plus the keys actually present, because "no list of
# projects" without the real keys sent me back to the cluster for a second look.
items = None
if isinstance(d, list):
    items = d
elif isinstance(d, dict):
    for key in ("values", "value", "items", "data"):
        if isinstance(d.get(key), list):
            items = d[key]
            break
if not isinstance(items, list):
    sys.exit("the /projects response carries no project list. Tried values/value/"
             "items/data; the keys actually present are: %s"
             % (sorted(d.keys()) if isinstance(d, dict) else type(d).__name__))
out = []
for it in items:
    md = (it or {}).get("metadata") or {}
    pid = md.get("id") or (it or {}).get("id")
    name = md.get("name") or (it or {}).get("name") or "?"
    if pid:
        out.append("%s\t%s" % (pid, name))
print("\n".join(out))
')" || die "could not read project ids from the response"
  PROJ_COUNT="$(printf '%s' "$PROJ_IDS" | grep -c . || true)"
  case "$PROJ_COUNT" in
    0) die "the account lists ZERO projects. There is nothing to create a Vpc in." ;;
    1) PROJECT="$(printf '%s' "$PROJ_IDS" | cut -f1)"
       note "one project on the account: ${PROJECT} ($(printf '%s' "$PROJ_IDS" | cut -f2))" ;;
    *) printf '%s\n' "$PROJ_IDS" | sed 's/^/    /'
       die "the account lists ${PROJ_COUNT} projects (above). Which one this probe should
write a Vpc into is an operator's choice, not this script's -- pass PROJECT=<id>." ;;
  esac
else
  note "PROJECT supplied: ${PROJECT}"
fi

# POSITIVE CONTROL on the id itself, whether resolved or supplied. This is the check whose
# absence once turned a deleted-CR-derived empty PROJECT into `/projects//providers/...`,
# eight collections reported unreadable and six about to be reported as leaked.
say "control: the project id resolves a real collection"
VPCS_RAW="$(aruba_get "/projects/${PROJECT}/providers/Aruba.Network/vpcs?api-version=1.0" || true)"
VPCS_CODE="$(printf '%s' "$VPCS_RAW" | sed -n 's/^HTTP:\([0-9][0-9]*\).*/\1/p' | head -1)"
[ "$VPCS_CODE" = "200" ] || die "GET vpcs in project ${PROJECT} returned HTTP ${VPCS_CODE:-<none>}.
The project id does not address a readable collection, so the Vpc child would fail for a
reason that has nothing to do with the rollup under test."
note "vpcs collection readable in ${PROJECT} (HTTP 200)"

say "oasgen version actually running (reported, never assumed)"
kubectl --context "$CTX" get deploy -n "$NS" \
  -o jsonpath='{range .items[*]}    {.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}' \
  | grep -E 'oasgen|rest-dynamic' || note "(no oasgen/rdc deployment matched -- report this verbatim)"

# ------------------------------------------------------------------------ core-provider
# TWO charts, crds first, both pinned to ONE version. The core-provider chart installs no
# CRDs at all -- measured: its manifest contains ServiceAccounts, ConfigMaps and a PDB, and
# `kubectl get crd` found nothing in core.krateo.io after a successful install, so the
# CompositionDefinition apply failed with "no matches for kind". Same split as
# oasgen-provider / oasgen-provider-crds, which pin_verify.sh already installs in that
# order.
#
# The version is resolved ONCE and used for both. Letting helm pick latest per chart would
# install two charts that are only coincidentally in step, and a release landing mid-run
# would split them -- installer has a whole gate about version lines for this reason.
if [ -z "$CORE_PROVIDER_VERSION" ]; then
  CORE_PROVIDER_VERSION="$(helm show chart \
    oci://ghcr.io/krateo-platformops/charts/core-provider 2>/dev/null \
    | awk '/^version:/{print $2; exit}')"
  [ -n "$CORE_PROVIDER_VERSION" ] || die "could not resolve the current core-provider chart
version, and this script will not let helm choose one per chart"
  note "resolved core-provider chart ${CORE_PROVIDER_VERSION}; both charts pinned to it"
fi

say "installing core-provider-crds ${CORE_PROVIDER_VERSION}"
helm --kube-context "$CTX" upgrade --install core-provider-crds \
  oci://ghcr.io/krateo-platformops/charts/core-provider-crds \
  --version "$CORE_PROVIDER_VERSION" \
  --namespace "$NS" --create-namespace --wait --timeout 5m \
  >/dev/null || die "core-provider-crds install failed"

# CONTROL: assert the CRD is SERVED before anything tries to apply one. Discovering this
# at apply time is what cost the previous run -- "no matches for kind" reads like a bad
# manifest, and the manifest was fine.
kubectl --context "$CTX" get crd compositiondefinitions.core.krateo.io >/dev/null 2>&1 \
  || die "core-provider-crds installed but compositiondefinitions.core.krateo.io is not
served. Nothing below can work; report this verbatim rather than retrying."
note "compositiondefinitions.core.krateo.io is served"

say "installing core-provider ${CORE_PROVIDER_VERSION}"
helm --kube-context "$CTX" upgrade --install core-provider \
  oci://ghcr.io/krateo-platformops/charts/core-provider \
  --version "$CORE_PROVIDER_VERSION" \
  --namespace "$NS" --create-namespace --wait --timeout 10m \
  >/dev/null || die "core-provider install failed"

# core-provider asked for the version read from the IMAGE TAG, not the chart: the chart
# version and the image it ships are not the same number, and the arm under test is in the
# image.
CP_IMAGE="$(kubectl --context "$CTX" get deploy -n "$NS" \
  -l app.kubernetes.io/name=core-provider \
  -o jsonpath='{.items[0].spec.template.spec.containers[0].image}' 2>/dev/null || true)"
[ -n "$CP_IMAGE" ] || die "core-provider installed but no deployment found to read an image from"
CP_TAG="${CP_IMAGE##*:}"
note "core-provider image ${CP_IMAGE}"
note "tag read for the floor check: ${CP_TAG} (floor ${CDC_FLOOR}, #496)"
# Refuse below the floor rather than measure a version whose childFailed arm predates the
# reasons under test. sort -V, and an explicit equality case so a floor-exact tag passes.
if [ "$CP_TAG" != "$CDC_FLOOR" ] \
   && [ "$(printf '%s\n%s\n' "$CP_TAG" "$CDC_FLOOR" | sort -V | head -1)" = "$CP_TAG" ]; then
  die "core-provider image tag ${CP_TAG} is BELOW the ${CDC_FLOOR} floor. #496 added
NoCreateVerbDeclared and CreateUnreachable to childhealth's childFailed arm; below that
tag the parent would read childConverging and the run would 'disprove' a contract that
simply is not implemented in this image."
fi

# ------------------------------------------------------- package, content-addressed
# The cdc fetches charts through helm.WithCache(): a disk cache keyed by URL+version with
# a 24h TTL. Rebuilding the chart and serving it at the same URL+version would be served
# from the cache, and the run would measure the PREVIOUS chart while every log line looked
# correct. Rather than rely on remembering to bump, the version IS the content: a digest
# over every file in the chart. A changed chart cannot collide with a cached one, and an
# UNCHANGED chart deliberately reuses the cache, which is harmless and fast.
say "packaging ${CHART_DIR} with a content-addressed version"
CONTENT_SHA="$(find "$CHART_DIR" -type f ! -name '*.tgz' -print0 \
  | LC_ALL=C sort -z \
  | xargs -0 shasum -a 256 \
  | shasum -a 256 | cut -c1-8)"
[ -n "$CONTENT_SHA" ] || die "could not digest the chart directory"
# `0.0.0-p<8 hex>` = 15 bytes, and the budget is the DERIVED NAMES, not the field.
#
# HISTORY, because the constraint changed under this script. core-provider capped
# `spec.chart.version` at 20 bytes, and this check enforced that. The cap measured the WRONG
# FIELD: the derived names come from the FETCHED chart's Chart.yaml version, not from
# spec.chart.version. core-provider 2.13.114 raises the field to maxLength 64 and checks the
# names that actually break -- verified here against 2.13.113 as a control: maxLength 20 at
# 2.13.113, 64 at 2.13.114.
#
# THE REAL LIMITS, which are plural-dependent:
#   <plural>-v<ver>-controller      <= 63  =>  plural + version <= 50
#   ...-controller-service          <= 63  =>  plural + version <= 42   (metrics only)
#
# The plural comes from the CHART NAME, so a descriptive name spends the budget first:
# `aruba-no-create-rollup-probe` -> kind `ArubaNoCreateRollupProbe` -> plural
# `arubanocreaterollupprobes` (25), leaving 17 for the version when metrics are on.
#
# CHECKED AGAINST THE TIGHTER BUDGET, deliberately. `cdc.metrics.enabled` is false in the
# chart's defaults, so 50 applies on a default cluster -- but a chart that only fits when a
# flag happens to be off is a chart that breaks on someone else's cluster. The default is
# not the constraint.
#
# AND NO CHARACTER-CLASS CHECK. An earlier note here said the CRD version must be DNS-1035
# so uppercase or `+` would be refused; core-provider withdrew that. crdgen's
# `NormalizeVersionName` lowercases and maps every other character to `-`, so `1.0.0-RC1`
# becomes `v1-0-0-rc1` and is NOT refused. Adding a character check would refuse versions
# the API accepts, which is the same error in the opposite direction.
PROBE_VERSION="0.0.0-p${CONTENT_SHA}"
# The plural crdgen will derive: the chart name with separators removed, lowercased, plus s.
PROBE_PLURAL="$(printf '%s' "$CHART_NAME" | tr -d '-' | tr '[:upper:]' '[:lower:]')s"
budget_tight=42 budget_loose=50
total=$(( ${#PROBE_PLURAL} + ${#PROBE_VERSION} ))
note "chart content digest ${CONTENT_SHA}"
note "version ${PROBE_VERSION} (${#PROBE_VERSION}) + plural ${PROBE_PLURAL} (${#PROBE_PLURAL}) = ${total}"
if [ "$total" -gt "$budget_tight" ]; then
  die "the derived name budget is exceeded: plural ${#PROBE_PLURAL} + version ${#PROBE_VERSION}
    = ${total}, against ${budget_tight} (the limit when cdc.metrics.enabled) and
    ${budget_loose} (without it). core-provider refuses this as
    Ready=False/DerivedNameTooLong, naming the derived name and its length.
    Shorten the DIGEST, not the prefix: the non-numeric prefix keeps the semver prerelease
    identifier from being all digits with a leading zero. Shortening the CHART NAME also
    works and buys more, since the plural is derived from it."
fi
note "within the tighter ${budget_tight}-byte budget, so it holds with or without metrics"
helm package "$CHART_DIR" --version "$PROBE_VERSION" --app-version "$PROBE_VERSION" \
  --destination "$WORK" >/dev/null || die "helm package failed"
TGZ="${CHART_NAME}-${PROBE_VERSION}.tgz"
[ -f "${WORK}/${TGZ}" ] || die "expected ${WORK}/${TGZ}; helm wrote: $(ls "$WORK")"
note "packaged ${TGZ} ($(wc -c < "${WORK}/${TGZ}" | tr -d ' ') bytes)"

# ------------------------------------------------------------------ in-cluster chart server
# A plain GET over Service DNS. core-provider confirmed from plumbing's helm/getter that a
# URL ending in .tgz takes the tgz getter -- a plain GET with no scheme or TLS requirement
# beyond http(s) -- and that `repo` is ignored for tgz. Both fetchers (core-provider, to
# generate the CRD, and the cdc/chart-inspector, to render and install) resolve it from
# inside the cluster, so Service DNS is correct for all of them.
#
# NOT published to GHCR, deliberately: this composition renders a child that can never be
# created, and that does not belong in a public registry where a user could install it.
say "serving the chart inside the cluster"
kubectl --context "$CTX" create configmap probe-chart -n "$NS" \
  --from-file="${WORK}/${TGZ}" --dry-run=client -o yaml \
  | kubectl --context "$CTX" apply -f - >/dev/null || die "chart ConfigMap failed"
kubectl --context "$CTX" apply -f - >/dev/null <<YAML || die "chart server failed"
apiVersion: apps/v1
kind: Deployment
metadata:
  name: probe-chart
  namespace: ${NS}
spec:
  replicas: 1
  selector: { matchLabels: { app: probe-chart } }
  template:
    metadata: { labels: { app: probe-chart } }
    spec:
      containers:
      - name: httpd
        image: ${HTTPD_IMAGE}
        ports: [ { containerPort: 80 } ]
        volumeMounts:
        - { name: chart, mountPath: /usr/share/nginx/html, readOnly: true }
      volumes:
      - name: chart
        configMap: { name: probe-chart }
---
apiVersion: v1
kind: Service
metadata:
  name: probe-chart
  namespace: ${NS}
spec:
  selector: { app: probe-chart }
  ports: [ { port: 80, targetPort: 80 } ]
YAML
kubectl --context "$CTX" rollout status deploy/probe-chart -n "$NS" --timeout=3m >/dev/null \
  || die "chart server did not become ready"
CHART_URL="http://probe-chart.${NS}.svc.cluster.local/${TGZ}"
# Prove the URL serves the bytes BEFORE handing it to cdc. Without this, an unreachable
# chart and a cdc that cannot parse one are the same symptom, and the second is a finding
# while the first is a typo.
# Fetch a URL from inside the cluster and print "HTTP:<code>" plus whatever the body was.
# Same apply/wait/logs shape as aruba_get, and for the same reason: `kubectl run --rm -i`
# is NOT a reliable way to capture a container's stdout here. Measured on this cluster --
# with stderr dropped it produced ZERO BYTES on stdout, so a `grep` over it could never
# match however well the fetch worked, and 18 retries all "failed" against a chart server
# that answered in under a millisecond when asked directly. `kubectl logs` reads what the
# container actually wrote.
chart_probe() {
  local url="$1" name="chart-check-${RANDOM}${RANDOM}" out=""
  kubectl --context "$CTX" apply -f - >/dev/null <<YAML || return 1
apiVersion: v1
kind: Pod
metadata:
  name: ${name}
  namespace: ${NS}
  labels: { app: chart-check }
spec:
  restartPolicy: Never
  containers:
  - name: curl
    image: ${CURL_IMAGE}
    env:
    - name: URL
      value: "${url}"
    command: ["sh","-c"]
    args:
    - |
      code=\$(curl -sS -m 10 -o /tmp/c.tgz -w '%{http_code}' "\$URL") || code=000
      printf 'HTTP:%s\\n' "\$code"
      # Prove it is a real gzipped chart and not an error page that happens to be 200.
      tar tzf /tmp/c.tgz 2>/dev/null | head -3 || echo "NOT-A-TARBALL"
YAML
  kubectl --context "$CTX" wait --for=jsonpath='{.status.phase}'=Succeeded \
    "pod/${name}" -n "$NS" --timeout=120s >/dev/null 2>&1 || true
  out="$(kubectl --context "$CTX" logs "pod/${name}" -n "$NS" 2>/dev/null || true)"
  kubectl --context "$CTX" delete "pod/${name}" -n "$NS" --wait=false >/dev/null 2>&1 || true
  printf '%s' "$out"
}

say "control: the chart URL serves a real tarball from inside the cluster"
# Retried because attempt 2 on this cluster hit a GENUINE connection timeout moments after
# `rollout status` returned, and the same URL answered later -- an unprogrammed ClusterIP
# drops packets rather than refusing them, so a few seconds of lag reads as a dead server.
# It still fails hard if it never succeeds: an unreachable chart and a chart the cdc
# refuses are the same symptom otherwise, and only the second is a finding.
chart_ok=0
for attempt in $(seq 1 12); do
  probe_out="$(chart_probe "$CHART_URL")"
  probe_code="$(printf '%s' "$probe_out" | sed -n 's/^HTTP:\([0-9][0-9]*\).*/\1/p' | head -1)"
  if [ "$probe_code" = "200" ] && ! printf '%s' "$probe_out" | grep -q 'NOT-A-TARBALL'; then
    chart_ok=1
    note "chart URL answered 200 with a readable tarball on attempt ${attempt}"
    printf '%s\n' "$probe_out" | sed -n '2,4p' | sed 's/^/      /'
    break
  fi
  note "attempt ${attempt}: code=${probe_code:-<none>}"
  sleep 10
done
[ "$chart_ok" = "1" ] || die "the chart URL never served a readable tarball from inside the
cluster: ${CHART_URL}
Last probe output was: ${probe_out:-<empty>}
An EMPTY probe output means the PROBE failed, not the server -- check the pod and its logs
before concluding anything about the chart. If the output shows a code, check in this order
because each rules out a different layer: the server over localhost
(\`kubectl exec deploy/probe-chart -- wget -S -O /dev/null http://127.0.0.1/<tgz>\`), then
pod-to-pod by pod IP, then pod-to-ClusterIP. Nothing below this point would be
interpretable, so stopping here."

# ------------------------------------------------------------- the CompositionDefinition
# NO spec.deploy.targetRef, deliberately. core-provider's deploy.IsClusterLocalChartURL
# flags *.svc, *.cluster.local and private IPs -- but only for a REMOTE definition, where a
# spoke genuinely cannot reach the hub's Service, and it reports RemoteChartUnreachable.
# A local definition is unaffected, and this one must stay local for that reason.
say "registering the CompositionDefinition"
kubectl --context "$CTX" apply -f - >/dev/null <<YAML || die "CompositionDefinition failed"
apiVersion: core.krateo.io/v1alpha1
kind: CompositionDefinition
metadata:
  name: no-create-rollup
  namespace: ${NS}
spec:
  chart:
    url: ${CHART_URL}
    # Documented as needed only for oci and helm-repo URLs, and set anyway because the
    # cdc's chart cache is keyed on URL+version. Here it is the content digest, so a
    # changed chart can never be served from a previous chart's cache entry.
    version: ${PROBE_VERSION}
YAML

# The generated CRD's group/version/kind are READ from status, never constructed. The
# naming rule is core-provider's, it has changed before, and a guessed kind fails as
# "no matches for kind" -- which reads like a broken release rather than a wrong guess.
say "waiting for the generated composition CRD"
CD_APIVER=""; CD_KIND=""
for _ in $(seq 1 60); do
  CD_APIVER="$(kubectl --context "$CTX" get compositiondefinition no-create-rollup -n "$NS" \
    -o jsonpath='{.status.apiVersion}' 2>/dev/null || true)"
  CD_KIND="$(kubectl --context "$CTX" get compositiondefinition no-create-rollup -n "$NS" \
    -o jsonpath='{.status.kind}' 2>/dev/null || true)"
  [ -n "$CD_APIVER" ] && [ -n "$CD_KIND" ] && break
  sleep 5
done
if [ -z "$CD_APIVER" ] || [ -z "$CD_KIND" ]; then
  say "the CompositionDefinition never reported a generated kind -- its own conditions:"
  kubectl --context "$CTX" get compositiondefinition no-create-rollup -n "$NS" \
    -o jsonpath='{range .status.conditions[*]}    {.type}={.status} reason={.reason} message={.message}{"\n"}{end}' || true
  die "no generated apiVersion/kind after 5 minutes. Report the conditions above verbatim --
a chart the fetcher could not read and a chart it read and refused are different findings."
fi
note "generated ${CD_APIVER} ${CD_KIND}"

# ------------------------------------------------------------------------ the Composition
say "applying the Composition (one no-create child + one that reconciles)"
kubectl --context "$CTX" apply -f - >/dev/null <<YAML || die "Composition failed"
apiVersion: ${CD_APIVER}
kind: ${CD_KIND}
metadata:
  name: no-create-rollup-probe
  namespace: ${NS}
spec:
  namespace: ${NS}
  projectId: "${PROJECT}"
  location: "${LOCATION}"
  name: probe
  tokenSecret:
    name: ${TOKEN_SECRET_NAME}
    namespace: ${TOKEN_SECRET_NS}
    key: token
  vpc:
    default: false
    preset: false
YAML

# Settle, rather than poll for the answer we want. Polling until the parent is false would
# report the first transient false -- including the normal Creating phase while the Vpc is
# still being made -- as the result. The Vpc needs to REACH Ready before the parent's
# verdict means anything, so wait on the contrast child first, then read once.
say "waiting for the Vpc child to reconcile (the contrast must be real)"
kubectl --context "$CTX" wait --for=condition=Ready "vpc/probe-vpc" -n "$NS" --timeout=10m \
  || note "the Vpc did NOT reach Ready within 10m -- the parent's message below may name
    BOTH children, which is a different (and weaker) result. Report it as such."
# One resync past that, so the parent has seen the settled children at least once.
sleep 90

# ------------------------------------------------------------------------------- the result
say "PARENT raw condition (${CD_KIND}/no-create-rollup-probe)"
kubectl --context "$CTX" get "${CD_KIND}" no-create-rollup-probe -n "$NS" \
  -o jsonpath='{range .status.conditions[*]}    type={.type} status={.status} reason={.reason}{"\n"}      message={.message}{"\n"}{end}' \
  || die "could not read the parent's conditions"
say "parent, verbatim YAML (the thing core-provider asked to be pasted)"
kubectl --context "$CTX" get "${CD_KIND}" no-create-rollup-probe -n "$NS" -o yaml \
  | sed -n '/^status:/,$p'

for child in "loadbalancer/probe-lb" "vpc/probe-vpc"; do
  say "CHILD ${child} raw conditions"
  kubectl --context "$CTX" get "$child" -n "$NS" \
    -o jsonpath='{range .status.conditions[*]}    type={.type} status={.status} reason={.reason}{"\n"}      message={.message}{"\n"}{end}' \
    || note "could not read ${child}"
done

say "versions this result belongs to (a condition without them is uninterpretable)"
note "core-provider image ${CP_IMAGE}"
note "oasgen pin        $(kubectl --context "$CTX" get deploy -n "$NS" \
  -o jsonpath='{range .items[*]}{.spec.template.spec.containers[0].image}{"\n"}{end}' \
  | grep -E 'oasgen-provider' | head -1)"
note "chart             ${TGZ}"

# ------------------------------------------------------------------------------- teardown
if [ "$KEEP" = "1" ]; then
  say "KEEP=1 -- leaving the Composition, the definition and the chart server in place"
  note "the Vpc child is a REAL billable-free vendor resource; delete the Composition to"
  note "remove it, or sweep /projects/${PROJECT}/providers/Aruba.Network/vpcs by hand"
  exit 0
fi
say "teardown"
# The Composition first: deleting the definition while children exist leaves the children
# with no controller, and a Vpc with no controller keeps its finalizer forever.
kubectl --context "$CTX" delete "${CD_KIND}" no-create-rollup-probe -n "$NS" \
  --timeout=10m >/dev/null 2>&1 || note "composition delete reported an error"
for _ in $(seq 1 60); do
  kubectl --context "$CTX" get vpc probe-vpc -n "$NS" >/dev/null 2>&1 || break
  sleep 10
done
if kubectl --context "$CTX" get vpc probe-vpc -n "$NS" >/dev/null 2>&1; then
  note "WARNING: vpc/probe-vpc still exists after the composition delete. Its vendor"
  note "resource may be leaked -- check /projects/${PROJECT}/providers/Aruba.Network/vpcs"
  note "BEFORE deleting the cluster, because deleting it strands the finalizer."
else
  note "vpc/probe-vpc gone"
fi
kubectl --context "$CTX" delete compositiondefinition no-create-rollup -n "$NS" >/dev/null 2>&1 || true
kubectl --context "$CTX" delete deploy/probe-chart svc/probe-chart configmap/probe-chart \
  -n "$NS" >/dev/null 2>&1 || true
note "done. The cluster itself is pin_verify.sh's to delete."
