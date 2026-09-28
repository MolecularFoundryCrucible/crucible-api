#!/usr/bin/env bash
set -euo pipefail

environment=${1:?Expected staging or main}
image=${2:?Expected the API SHA-tagged image}
case "$environment" in
  staging|main) ;;
  *) echo "Expected staging or main" >&2; exit 2 ;;
esac
case "$image" in
  *:latest|*:) echo "Expected an immutable release image reference" >&2; exit 2 ;;
esac

manifest="k8s-conf/hub-auth-cleanup-${environment}.yaml"
name="crucible-hub-auth-cleanup-${environment}"
work_dir=$(mktemp -d)
trap 'rm -f "$work_dir/rendered.json" "$work_dir/update.json"; rmdir "$work_dir"' EXIT

kubectl set image --local -f "$manifest" "cleanup=$image" -o json > "$work_dir/rendered.json"
existing=$(kubectl get cronjob "$name" --namespace default --ignore-not-found -o name)
if [[ -z "$existing" ]]; then
  kubectl create --namespace default -f "$work_dir/rendered.json"
else
  kubectl patch --local -f "$work_dir/rendered.json" --type=json \
    -p '[{"op":"remove","path":"/spec/suspend"}]' -o json > "$work_dir/update.json"
  kubectl patch cronjob "$name" --namespace default --type=merge --patch-file "$work_dir/update.json"
fi
