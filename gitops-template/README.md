# gitops-template

Seed for the GitOps repo that Argo CD pulls. Copy it into the GitOps repo and replace `GITOPS_REPO` with that repo's URL.

- `clusters/aws/platform/` — App of Apps root path (created on the node by `infra/ansible`): CNPG, External Secrets, tenant AppProject and ApplicationSet, and the cluster network baseline (`30-network-baseline.yaml`, see `platform/ZERO-TRUST.md`).
- `apps/aws/<tenant>/<app>/` — renderer output (`platform/render/render.py`). The ApplicationSet turns each directory into one Application in namespace `t-<tenant>-<app>`.
