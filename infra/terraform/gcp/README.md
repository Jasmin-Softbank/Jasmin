# GCP single app node

This Terraform root provisions one Ubuntu 24.04 amd64 Compute Engine VM in an **existing explicit project, region and zone**, with a dedicated VPC/subnet, reserved public IP, optional TCP 80/443 rule, a dedicated service account, and a separate Persistent Disk. It bootstraps the shared k3s/Argo CD playbook. It does not create a product control plane, user workspace, CI runner, GKE cluster, cloud project, or API credentials.

Default compute is `e2-standard-4` (4 vCPU / 16 GiB), with a 30 GiB boot disk and 100 GiB `pd-balanced` data disk. Supported alternatives are `e2-standard-2` and `e2-standard-8`. No GPU, HA, multi-node networking, snapshots, backup restoration, or CSI driver is implemented. An optional per-start runtime limit can stop a short-lived test VM; it is disabled by default. This profile remains **live-unverified** until the target-specific deployment and acceptance checks below pass.

## Prerequisites and boundaries

- Terraform 1.5.7 or later, below 2.0; Google provider is fixed to **8.5.0**, with `.terraform.lock.hcl`. Static render checks also need Python 3 with PyYAML and Bash. Guest bootstrap pins `ansible-core==2.21.4` inside a Python venv; Ubuntu 24.04 supplies Python 3.12.
- The operator prepares billing, quota, `compute.googleapis.com` and `iam.googleapis.com` in the existing project. The module deliberately does not enable or disable project APIs. The target project ID and zone must be reviewed before planning.
- The administrator executor needs Compute instance/disk/address, VPC/subnet/firewall, and service-account create/read/update/delete permissions, plus `iam.serviceAccounts.actAs` on the node account. Typical provisioning roles are `roles/compute.instanceAdmin.v1`, `roles/compute.networkAdmin`, `roles/compute.securityAdmin`, `roles/iam.serviceAccountAdmin` and `roles/iam.serviceAccountUser`; narrow these to the required target scope. These are **operator permissions**, never VM permissions. Any API-enablement permissions belong to the separate project setup.
- Use approved Application Default Credentials or workload identity federation on that executor. Do not put tokens, service-account key JSON, Git credentials, or Codex authentication into tfvars, bootstrap files, state, or metadata.
- The dedicated VM service account has **no IAM role bindings and no OAuth scopes** in this module. Project/organization policy can still affect authorization and must be checked for the target. Public package, GitHub and image pulls require no Google API access.
- No SSH firewall rule exists by default. `allow_iap_ssh = true` adds TCP 22 **only** from Google's `35.235.240.0/20` IAP forwarding range, targeting this VM's service account. This is still management inbound on TCP22, not an outbound-only transport; keep it false for the strict zero-management-ingress profile. It never opens SSH to all Internet sources and sets `transport_ref` to an `iap:<project>/<zone>/<name>` reference; the default remains `null`. OS Login is enabled and project SSH keys are blocked. Firewall access alone does not grant login: separately enable the IAP/OS Login APIs as required and grant the operator `roles/iap.tunnelResourceAccessor`, `roles/compute.osLogin` (or `osAdminLogin` for privileged checks), and `roles/iam.serviceAccountUser` on the attached service account. The module creates no such user bindings. Read-only serial-port output can help diagnose initial boot; interactive serial console is disabled. [IAP setup](https://docs.cloud.google.com/iap/docs/using-tcp-forwarding), [OS Login roles](https://docs.cloud.google.com/compute/docs/oslogin/set-up-oslogin).
- HTTP and HTTPS ingress are both closed by default. Enable only the required ports and review source CIDRs. Opening 443 does not provision a certificate or TLS listener. The dedicated VPC keeps Google's implied egress allow rule; this initial module is not a complete egress allowlist implementation. The public IP enables bootstrap egress without Cloud NAT and remains billable while reserved, including VM stop.

## Bootstrap source

Use either of these administrator-controlled modes:

1. **Published public Git:** set `node_repo` to a public GitHub HTTPS URL and `node_ref` to the reviewed, published 40-character Git commit. That commit must contain the GCP-capable `infra/ansible/node.yml`; a local edit or an older AWS-only commit is insufficient. Image families and floating node branches are rejected. The GitOps repo must also be public; set `gitops_path = "clusters/gcp/platform"` to a path that actually exists, and explicitly choose its branch/tag/commit. A branch can track later CD commits; a commit freezes the bootstrap test.
2. **Unpublished public code bundle:** leave `node_repo`/`node_ref` empty and supply `bootstrap_files` as a map of base64 file contents, with exactly `node.yml`, `files/cilium.yaml.j2`, `files/traefik-config.yaml.j2`, `files/argocd.yaml.j2`, and `files/root-app.yaml.j2`. cloud-init writes them beneath the fixed `/opt/railshot/bootstrap` directory and runs `ansible-playbook`. This path is for reviewed non-secret code, not arbitrary user uploads. Base64 is not encryption. Review the exact map and SHA-256 in the saved plan; the descriptor and `/etc/railshot/bootstrap-manifest.json` record `sha256(jsonencode(bootstrap_files))`. No remote commit is claimed in bundle mode.

Both modes write `name`, `cloud_provider: gcp`, `region`, `gitops_repo`, `gitops_path` and `gitops_revision` to `/etc/railshot/node.yml`. They contain no AWS SSM references. Private repos/registries are unsupported until a separate scoped secret-delivery path is implemented.

cloud-init runs the bootstrap on first boot. Changing metadata, bundle bytes or Git revision on an existing VM **does not automatically rerun cloud-init**. Use a separately reviewed configure operation or planned VM replacement after drain/backup. Do not use `cloud-init clean` as an unreviewed update mechanism. `/var/lib/railshot/bootstrap-status.json` records only completion of the Ansible command; application and Argo CD health require live checks. `node_descriptor.bootstrap.status` remains `unverified` until an observation layer has evidence.

## Data lifetime

The data disk is a separate Terraform resource, attached as `/dev/disk/by-id/google-railshot-data` and mounted at `/var/lib/rancher` **before** running Ansible. This covers k3s state and the default local-path storage directory. Google-managed encryption applies by default. The module does not supply encryption keys.

`initialize_empty_data_disk = true` is explicit consent for the first format of this module's new, blank, unpartitioned disk. Existing ext4 is reused without formatting; other filesystems, partition tables, unknown signatures, a wrong mounted UUID, or preexisting data beneath an unmounted mount point cause bootstrap failure. This is not a general import/repair tool: do not use the initialization flag on an imported or damaged disk. `/etc/fstab` uses the filesystem UUID and intentionally omits `nofail`; a missing data disk must not silently start an empty database. A k3s systemd drop-in also requires `/var/lib/rancher` to be mounted.

The compute attachment does not auto-delete the data disk. `prevent_destroy = true` blocks Terraform plans that replace/destroy the disk, and the provider's state-recorded `deletion_policy = "PREVENT"` adds a removal guard. These are Terraform protections, not protection against an authorized console/API deletion. Removing the whole stack with `terraform destroy` is intentionally blocked. A deliberate data deletion requires separate backup/retention approval and explicitly changing both guards; routine compute replacement must preserve this disk.

Compute replacement in the **same zone** can reattach the retained disk after the old VM is removed. It causes downtime and must not overlap writers. Review k3s version compatibility, stable node identity, quiescing/checkpointing, DB backups and restore evidence before replacement. Moving zones or changing disk name is not a transparent migration. Disk capacity increases also need a separately verified filesystem resize; Terraform alone does not grow ext4. Retaining block data does not replace backups, HA, Pod/PVC retention policy, or an application-consistent restore test.

## Optional test runtime limit

`max_run_duration_seconds = 7200` schedules `STOP` after two hours of VM run time, with `automatic_restart = false` on this standard VM. The accepted range is 30 seconds to 120 days; `null` adds no scheduling override. This preserves Persistent Disks and does not drain workloads, checkpoint jobs, create snapshots, or remove disk/IP charges. Google can begin stopping up to 30 seconds after the limit. The duration is recalculated on each stop/start; an OS reboot does not reset it. It is therefore a per-start guard, not an absolute spending cap. [Google runtime limits](https://docs.cloud.google.com/compute/docs/instances/limit-vm-runtime).

The module deliberately leaves `desired_status` unset. A later executor must observe a cutoff stop as `TERMINATED`, preserve the stopped lifecycle state and require an explicit start decision. Adding `desired_status = "RUNNING"`, replacing the instance, or a reconciler blindly restarting stopped VMs can undo the cost guard and start another full duration. Review the live state and saved plan before every apply. Changing `max_run_duration_seconds` on a running VM also requires a separate stop/maintenance operation because automatic stopping for updates remains disabled. The cutoff and its stop/start semantics require live verification; Terraform validation alone does not prove them.

After enabling IAP and preparing operator IAM, an explicit connection can use:

```bash
gcloud compute ssh NODE_NAME --project=PROJECT_ID --zone=ZONE --tunnel-through-iap
```

## Local checks and operator plan

From this directory, the following checks do not access a GCP project:

```bash
terraform init -backend=false -input=false
terraform fmt -check -recursive
terraform validate
python3 test_bootstrap.py
```

The render tests cover both bootstrap modes, fixed bundle paths, the pinned Git commit, provider-neutral node configuration, mount-before-Ansible ordering, shell syntax, and the default/opt-in IAP and runtime policies. They do not execute disk formatting, Ansible, GCP APIs or k3s.

For a live deployment, establish approved credentials separately, copy the example and replace every placeholder. Select a real exact Ubuntu image; do not paste an image-family reference. Protect state/plan files; local state is acceptable only for a single operator PoC. A shared executor requires a separately owned encrypted/versioned remote backend with locking and recovery checks.

```bash
cp terraform.tfvars.example terraform.tfvars
# Edit the explicit target, exact image, bootstrap source, GitOps path and web ingress.
terraform init -input=false
terraform plan -input=false -out=reviewed.tfplan
terraform show -no-color reviewed.tfplan
# Review resource scope, data preservation, public ingress, bootstrap digest and cost.
terraform apply reviewed.tfplan
terraform output -json node_descriptor
```

Apply only the reviewed saved plan on the administrator executor; keep it and the state out of Git and user CI. `allow_stopping_for_update = false` prevents silently stopping this stateful VM for a resize; resize is a separate reviewed maintenance operation. The module adds six resources by default, one more for web ingress when enabled, and one more for IAP SSH when enabled; the VM boot disk is created as part of the compute instance.

Live acceptance is separate from `validate`: confirm actual project/zone/image/SA/ingress rules, VM and attached disk IDs, `cloud-init status --wait`, mounted disk UUID, bootstrap manifest hash, running pinned k3s version and Ready node, Argo CD root application Synced/Healthy, and an app reachable only on approved ports. Guest checks need approved IAP access or another administration transport. If the runtime limit is enabled, read back `scheduling.maxRunDuration`, `instanceTerminationAction`, `automaticRestart`, the termination timestamp, and eventual `TERMINATED` status. Then verify an explicitly authorized stop/start and controlled compute replacement preserve a test record and the data disk ID; do not claim data recovery from a static plan. Record PASS/FAIL/BLOCKED/NOT_RUN and exact observations.

## Official references

- [Google provider 8.5.0 compute instance](https://github.com/hashicorp/terraform-provider-google/blob/v8.5.0/website/docs/r/compute_instance.html.markdown) and [disk deletion policy](https://github.com/hashicorp/terraform-provider-google/blob/v8.5.0/website/docs/r/compute_disk.html.markdown).
- [Compute Engine service accounts and scopes](https://docs.cloud.google.com/compute/docs/access/service-accounts).
- [Google disk formatting, persistent device names and UUID mounts](https://docs.cloud.google.com/compute/docs/disks/format-mount-disk-linux).
- [cloud-init GCE user-data metadata](https://docs.cloud-init.io/en/latest/reference/datasources/gce.html).
