# Clawless Kubernetes Deployment

Kustomize-based deployment for [Clawless](https://github.com/srinathh/clawless), a personal AI assistant backed by the Claude Agent SDK.

## Prerequisites

- Kubernetes cluster (tested on MicroK8s 1.35)
- `kubectl` with kustomize support (`kubectl apply -k`)
- Container image available at `ghcr.io/srinathh/clawless` (public, no pull secrets needed)
- `ANTHROPIC_API_KEY` for the Claude API

## Quick Start

```bash
# 1. Copy templates and fill in your values
cp configmap-template.yaml configmap.yaml
cp secret-template.yaml secret.yaml
$EDITOR configmap.yaml   # channel config, claude settings
$EDITOR secret.yaml       # API keys, Twilio credentials

# 2. Deploy
kubectl apply -k .

# 3. Verify
kubectl -n clawless get pods -w
kubectl -n clawless logs deploy/clawless -f
```

## File Layout

| File | Committed | Purpose |
|---|---|---|
| `namespace.yaml` | Yes | `clawless` namespace |
| `pvc.yaml` | Yes | 5Gi PersistentVolumeClaim (uses cluster default StorageClass) |
| `deployment.yaml` | Yes | Deployment: 1 replica, Recreate strategy, init container for scaffolding |
| `service.yaml` | Yes | ClusterIP Service on port 18265 |
| `kustomization.yaml` | Yes | Kustomize entrypoint — image tag, StorageClass patch, resource list |
| `configmap-template.yaml` | Yes | Template for ConfigMap (non-sensitive config) |
| `secret-template.yaml` | Yes | Template for Secret (API keys, Twilio creds) |
| `configmap.yaml` | **No** (gitignored) | Your actual ConfigMap — copy from template |
| `secret.yaml` | **No** (gitignored) | Your actual Secret — copy from template |

## Architecture

The deployment uses a single PVC with `subPath` mounts for all data directories:

```
PVC (clawless-home)
├── .claude/       → /home/clawless/.claude      (rw) SDK runtime state
├── workspace/     → /home/clawless/workspace    (rw) agent working directory
├── data/          → /home/clawless/data         (rw) SQLite database
├── logs/          → /home/clawless/logs         (rw) application logs
└── plugin/        → /home/clawless/plugin       (ro) pre-configured plugin
```

An **init container** runs `clawless-init` on every start to scaffold these directories inside the PVC. It mounts the raw PVC at `/mnt/clawless-home` because the subdirectories may not exist on first run. The main container then mounts the individual subdirectories into `/home/clawless/` via subPath. The init is idempotent — it only creates directories and template files if they don't already exist.

Configuration (`clawless.toml`) is mounted from a ConfigMap. Secrets are injected as environment variables — pydantic-settings reads them with `__` as the nested delimiter (e.g. `CHANNELS__TWILIO_WHATSAPP__ACCOUNT_SID`).

## Customization via kustomization.yaml

**Image tag**: change `newTag` under the `images` section.

**StorageClass**: the base PVC omits `storageClassName` (uses cluster default). The kustomization patches in `microk8s-hostpath` — remove or change the patch for other clusters.

**Port**: a commented-out patch block shows how to override the default port (18265).

**Node pinning**: the deployment uses `nodeSelector: { role: primary }`. Edit `deployment.yaml` to change this for your cluster.

## External Access

The service is `ClusterIP` only — no ingress is included. For external access, point your tunnel (e.g. cloudflared) or ingress controller at `clawless.clawless.svc.cluster.local:18265`.

## Updating

```bash
# After a new image is pushed to GHCR:
kubectl -n clawless rollout restart deployment/clawless

# After editing configmap.yaml:
kubectl apply -k .
kubectl -n clawless rollout restart deployment/clawless
```

## Source

These manifests live in the [clawless repo](https://github.com/srinathh/clawless) at `deploy/k8s/` and are symlinked into this directory.
