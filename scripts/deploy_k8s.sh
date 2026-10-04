#!/usr/bin/env bash
# Update every long-running workload built from the lkm-service image.
# prefect-worker registers deployments on startup, so each rollout refreshes schedules.
set -euo pipefail

image=${1:?usage: deploy_k8s.sh IMAGE [NAMESPACE]}
namespace=${2:-lkm}

if [[ "$image" != *@sha256:* && ! "$image" =~ :[0-9a-f]{40}$ ]]; then
  echo 'Expected an immutable digest or 40-character commit SHA image tag' >&2
  exit 2
fi

deployments=(
  auth backend worker worker-send worker-notify worker-notification
  worker-content-index worker-dlq worker-outbox
  worker-points-reward worker-points-stats worker-points-tasks
  prefect-server prefect-worker
)

# Validate the entire target set before the first mutation. Missing workloads
# mean the cluster manifest does not match this release, so stop without a
# partial image update.
for name in "${deployments[@]}"; do
  kubectl -n "$namespace" get "deployment/$name" -o name >/dev/null
  actual=$(kubectl -n "$namespace" get "deployment/$name" \
    -o "jsonpath={.spec.template.spec.containers[0].name}")
  if [[ "$actual" != "$name" ]]; then
    echo "Unexpected container in deployment/$name: $actual" >&2
    exit 1
  fi
done

for name in "${deployments[@]}"; do
  kubectl -n "$namespace" set image "deployment/$name" "$name=$image"
done

for name in "${deployments[@]}"; do
  replicas=$(kubectl -n "$namespace" get "deployment/$name" \
    -o 'jsonpath={.spec.replicas}')
  if [[ "$replicas" == 0 ]]; then
    echo "deployment/$name is scaled to zero; image updated, rollout deferred"
    continue
  fi
  kubectl -n "$namespace" rollout status "deployment/$name" --timeout=180s
done

echo "Deployed $image to namespace $namespace"
