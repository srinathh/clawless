# Clawless — Deployment Guide

End-to-end reference for building, releasing, and operating Clawless on the
home Kubernetes cluster.

## Overview

```
feature branch
    │
    ▼
merge to main → merge to release → git push origin main release
                                        │
                                        ▼
                              GHA: docker-publish.yaml
                              builds & pushes to ghcr.io/srinathh/clawless:latest
                                        │
                                        ▼
                              kubectl rollout restart deployment/clawless -n clawless
```

---

## 1. Building and releasing a new image

The GHA workflow (`.github/workflows/docker-publish.yaml`) triggers on pushes
to the `release` branch and tags matching `v*`. It builds and pushes to GHCR.

### Standard release flow

```bash
# 1. Merge your feature branch to main
git checkout main
git merge feature/your-feature --no-ff -m "Merge feature/your-feature: <summary>"

# 2. Merge main to release (triggers GHA build)
git checkout release
git merge main --no-ff -m "Merge main: <one-line summary>"

# 3. Push both
git push origin main release

# 4. Watch the build
gh run list --limit 3
gh run watch <run-id>
```

The `release` push triggers one GHA job (`build-and-push`) which:
- Logs in to `ghcr.io` using `GITHUB_TOKEN`
- Builds with Buildx (layer cache via GHA cache)
- Pushes with two tags: `latest` (always) and `sha-<short-sha>`

Typical build time: **~1–2 min** (cache warm), **~3–4 min** (cache cold).

### Tagged release (semver)

```bash
git tag v1.2.3
git push origin v1.2.3
```

Pushes an additional `1.2.3` tag alongside `sha-*`.

---

## 2. Kubernetes cluster

### Context

```
kubectl config current-context   # → home_k8s
```

Namespace: `clawless`

### Manifests

All manifests live in `deploy/k8s/`. Applied via Kustomize:

```
deploy/k8s/
├── namespace.yaml           # clawless namespace
├── deployment.yaml          # main workload (Recreate strategy, node: role=primary)
├── service.yaml             # ClusterIP on port 18265
├── pvc.yaml                 # (if present) persistent volume claim
├── configmap-template.yaml  # template — copy to configmap.yaml and edit
├── configmap.yaml           # gitignored — clawless.toml content as ConfigMap
├── secret-template.yaml     # template — copy to secret.yaml and fill in keys
├── secret.yaml              # gitignored — ANTHROPIC_API_KEY and Twilio creds
└── kustomization.yaml       # image tag override + hostPath patch for this cluster
```

`secret.yaml` and `configmap.yaml` are gitignored and must never be committed.
They contain real API keys and per-cluster config.

### Host directory

The deployment mounts a `hostPath` volume at `/home/srinathh/clawless_home`
(set in `kustomization.yaml`). The init container runs `clawless-init` against
this path on every pod start (idempotent).

Subdirectories mounted into the container:

| Host path | Container path | Mode |
|---|---|---|
| `clawless_home/.claude` | `/home/clawless/.claude` | rw |
| `clawless_home/workspace` | `/home/clawless/workspace` | rw |
| `clawless_home/data` | `/home/clawless/data` | rw |
| `clawless_home/logs` | `/home/clawless/logs` | rw |
| `clawless_home/plugin` | `/home/clawless/plugin` | ro |

`clawless.toml` is mounted from the `clawless-config` ConfigMap.
Secrets (`ANTHROPIC_API_KEY`, channel credentials) come from `clawless-secrets`.

---

## 3. Deploying a new image to the cluster

The deployment uses `imagePullPolicy: Always` and the `latest` tag, so a
rollout restart is all that's needed after a new image is pushed.

```bash
kubectl rollout restart deployment/clawless -n clawless
kubectl rollout status deployment/clawless -n clawless --timeout=120s
```

The strategy is `Recreate` (old pod terminates before new pod starts) — safe
because the app is single-replica and stateful (SQLite on hostPath).

### Applying manifest changes

For changes to `deployment.yaml`, `service.yaml`, `configmap.yaml`, etc.:

```bash
kubectl apply -k deploy/k8s/
```

---

## 4. First-time cluster setup

```bash
# 1. Scaffold host directory (as the host user, not root)
clawless-init /home/srinathh/clawless_home
# or on the node:
# ssh <node> "mkdir -p /home/srinathh/clawless_home"
# then run clawless-init locally with HOME set, or let the init container do it

# 2. Create secret.yaml from template
cp deploy/k8s/secret-template.yaml deploy/k8s/secret.yaml
$EDITOR deploy/k8s/secret.yaml   # fill in ANTHROPIC_API_KEY and Twilio fields

# 3. Create configmap.yaml from template
cp deploy/k8s/configmap-template.yaml deploy/k8s/configmap.yaml
$EDITOR deploy/k8s/configmap.yaml   # set allowed_senders, ack_message, etc.

# 4. Apply everything
kubectl apply -k deploy/k8s/

# 5. Verify
kubectl rollout status deployment/clawless -n clawless
kubectl get pods -n clawless
kubectl logs -n clawless deployment/clawless --follow
```

---

## 5. Operations

### Health check

```bash
kubectl exec -n clawless deployment/clawless -- curl -s http://localhost:18265/health
# → {"status": "ok"}
```

### Logs

```bash
kubectl logs -n clawless deployment/clawless --follow
# Logs are also tailed to clawless_home/logs/clawless.log on the host node
```

### Wiki

The agent's markdown wiki is served at `/wiki` (e.g. via port-forward or
ingress). The agent writes pages to `~/workspace/wiki/` and they appear
immediately without a restart.

```bash
kubectl port-forward -n clawless svc/clawless 18265:18265
# then open http://localhost:18265/wiki
```

### Updating secrets or config

```bash
$EDITOR deploy/k8s/secret.yaml      # or configmap.yaml
kubectl apply -k deploy/k8s/
kubectl rollout restart deployment/clawless -n clawless
```

### Checking image digest

```bash
kubectl get deployment clawless -n clawless \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

---

## 6. CI/CD — GHA workflow reference

File: `.github/workflows/docker-publish.yaml`

| Trigger | Tag pushed |
|---|---|
| Push to `release` branch | `latest`, `sha-<short>` |
| Push tag `v*` | `<semver>`, `sha-<short>` |

Registry: `ghcr.io/srinathh/clawless`

Auth: `GITHUB_TOKEN` (automatic, no manual secret needed).

Build cache: GHA cache (`type=gha,mode=max`) — speeds up repeated builds by
caching Docker layers between runs.

Node.js 20 deprecation warning is cosmetic — actions still run correctly.
Will need `actions/checkout@v5` etc. before September 2026.
