# Azure app node

This Terraform root provisions one Ubuntu 24.04 amd64 VM for the shared k3s/Argo CD bootstrap. It creates a dedicated VNet/subnet, NSG, NIC, Standard static public IP, system-assigned managed identity, and a separate managed data disk. It does not provision the product control plane, personal CI workspace, AKS, or a cloud credential.

## Administrator inputs and ownership

- `resource_group_mode = "existing"` is the default: supply an administrator-registered existing resource group in the explicit `subscription_id`. The module does not import or own that resource group. `"create"` explicitly creates a dedicated group with Terraform deletion protection; changing modes later requires a reviewed state migration. Never select another project's group or reuse conflicting resource names.
- Authenticate the execution environment with the administrator's approved Azure identity or workload federation. Credentials and backend access keys are not module inputs. Configure an encrypted, locked remote backend before shared operation; local state and plans must remain private. State includes configuration, public keys, resource IDs and custom data.
- Resource provider registration is disabled (`resource_provider_registrations = "none"`). The administrator must register `Microsoft.Compute` and `Microsoft.Network` in advance and grant the executor only the target-scope permissions it needs. Creating a new resource group also needs appropriate subscription-scope permission.
- The VM identity receives **no Azure RBAC assignments**. This module does not implement Key Vault delivery, an Azure command transport, or a credential broker. `node_descriptor.transport_ref` is a reference for the future registered management path, not a working endpoint.
- Only D2s/D4s/D8s_v5 x86 SCSI profiles are accepted. Verify regional size availability, quota and the exact numeric Marketplace image version before a live plan. The fixed image tuple is `Canonical:ubuntu-24_04-lts:server:<image_version>`; `latest` is rejected. Image pins may disappear from the Marketplace, so recheck availability before replacement.
- NSG inbound is closed by default, including SSH from both public and VNet sources, Kubernetes API and Argo UI. Explicit `web_ports = [80, 443]` opens only those app ports to `web_source_cidrs`; it does not issue TLS certificates. The local public SSH key is required by the VM API but does not open an SSH route. No Bastion is created.
- Azure default outbound access rules remain in place. The public IP supplies explicit bootstrap egress; a complete domain/IP egress allowlist is not implemented here. Outbound access is needed for Ubuntu packages, PyPI, public Git and pinned runtime/chart downloads.

## Public, pinned bootstrap

Copy `terraform.tfvars.example` to an ignored local tfvars file and replace every placeholder. `node_ref` must be a reviewed, **published 40-character commit SHA** in the public `node_repo`, containing the Azure-aware shared `infra/ansible/node.yml`. Local unpublished changes or an old AWS-only revision are insufficient. The GitOps repository must also be public and contain the selected `gitops_path`; the default path is `clusters/azure/platform`. `gitops_revision` is the explicitly selected GitOps branch/tag/commit. Private repository and registry authentication require a separately implemented scoped secret path; no token belongs in tfvars, custom data, source, or logs.

cloud-init writes `/etc/railshot/node.yml` with `name`, `cloud_provider: azure`, `gitops_repo`, `gitops_path`, and `gitops_revision`. It waits up to ten minutes for the separate SCSI disk at LUN 0, mounts it at `/var/lib/rancher`, installs `ansible-core==2.21.4` in a Python venv, and invokes `ansible-pull` at the pinned commit. The shared playbook owns k3s/Cilium/Traefik bootstrap/Argo/root Application; Argo CD owns subsequent application CD. No AWS SSM parameters are passed.

Only a signature-free disk is initially formatted. An existing ext4 filesystem is reused; an unexpected filesystem, partition signature, conflicting fstab entry, populated unmounted target directory, or wrong mounted UUID stops bootstrap. A k3s systemd drop-in requires the persistent mount before the service starts. This protects against silently writing app state onto the temporary OS filesystem; it is not a backup or proof of application data consistency.

Azure may report VM creation success before cloud-init completes. If disk attachment or bootstrap fails, inspect the guest cloud-init result through the separately authorized Azure management path, fix the cause, and rerun `/usr/local/sbin/railshot-bootstrap` only after reviewing the same pinned inputs. Do not report `terraform apply` as k3s/Argo readiness. The descriptor deliberately returns `bootstrap.readiness = "unverified"` and `storage.backup_verified = false` until an observation/backup workflow supplies evidence.

## Lifecycle and cost boundaries

| Action | Compute and retained state |
|---|---|
| Azure guest shutdown / Stopped | Allocated compute can remain billable. No RAM resume contract is provided. |
| Azure deallocate | Releases compute allocation; persistent disks and static public IP remain and can incur charges. Start/deallocate operations are not implemented by this Terraform root. |
| `compute_enabled = false` | A reviewed apply deletes the VM, its OS disk and data attachment. It retains the managed data disk, RG and network/IP. This is compute deletion, not pause or deallocation. |
| `compute_enabled = true` after removal | Creates a new OS disk/VM and managed identity, then reattaches data. Bootstrap reuses ext4. Restore and k3s identity/version compatibility still require a live acceptance test. |
| Image / node commit / custom-data change | Can force VM replacement. Review the plan, drain workloads, verify backup/restore, and accept downtime before applying. This single-node profile has no HA. |
| `terraform destroy` / resource rename | Data disk `prevent_destroy` blocks planned deletion; an explicitly created RG also has this guard. Neither guard protects against out-of-band Azure deletion or removing the guarded resource block from configuration. |

`/var/lib/rancher` retains k3s state and default local-path volume content. This does not preserve arbitrary host paths, external databases, RAM, or every application's storage. Pod rollback does not restore schema/data. Namespace, PVC, database and provider-disk deletion remain separate approved operations. The data disk cannot be shrunk; Terraform disk expansion does not automatically grow ext4. Keep resource name/hostname stable and perform reviewed filesystem/database migration when necessary.

Before final retirement: stop dispatch, drain workloads, reconcile in-flight GitOps work, verify resource ownership and dependent PVC/DBs, obtain separate deletion approval and a tested backup receipt, then retire compute and finally data/network resources. Do not remove `prevent_destroy` merely to make a failed destroy succeed. This module does not implement that product orchestrator or backup process. VM recreation changes the system-assigned principal; future scoped RBAC bindings must be reconciled against the observed principal.

KEDA can scale supported workloads after cluster installation; it does not scale this single VM, its disk, or the Terraform resource graph. No VM autoscaler, billing cap, forced expiry timer or cost estimate is implemented here. Budget VM hours, managed/OS disks, retained public IP and traffic separately; deallocation does not reduce every cost to zero.

## Local validation and live acceptance

Run from this directory:

```sh
terraform init -backend=false -input=false -no-color
terraform fmt -check -recursive
terraform validate -no-color
python3 tests/test_bootstrap.py
```

The render test needs Python 3 with PyYAML, Terraform and Bash. It evaluates the actual Terraform template, parses its YAML, checks the typed Ansible handoff and runs Bash syntax validation without executing guest commands. AzureRM is fixed to 5.7.0 in `versions.tf` and the dependency lock; Terraform supports 1.5.7 through 1.x. Guest Ubuntu/Python/Ansible compatibility and provider APIs still require live validation.

No Azure account, live plan, resource creation, bootstrap run, data restore or Argo sync was validated during this implementation. Before enabling a target, record separate evidence for saved-plan ownership review, VM/disk/NSG readback, `cloud-init status --wait`, actual mount UUID, k3s Ready/version, Argo source revision/sync/health/image digest, second-run idempotency, and compute removal/recreation with verified data. Run only on an explicitly registered test target with a reviewed budget.

## Primary references

- [AzureRM 5.7.0 Linux VM schema](https://github.com/hashicorp/terraform-provider-azurerm/blob/v5.7.0/website/docs/r/linux_virtual_machine.html.markdown), [managed disk schema](https://github.com/hashicorp/terraform-provider-azurerm/blob/v5.7.0/website/docs/r/managed_disk.html.markdown), [disk attachment schema](https://github.com/hashicorp/terraform-provider-azurerm/blob/v5.7.0/website/docs/r/virtual_machine_data_disk_attachment.html.markdown)
- [Canonical Ubuntu image URNs](https://ubuntu.com/azure/docs/azure-how-to/instances/find-ubuntu-images/) and [image retention](https://ubuntu.com/azure/docs/azure-explanation/image-retention-policy/)
- [Azure custom data and cloud-init completion](https://learn.microsoft.com/en-us/azure/virtual-machines/custom-data), [VM power states and billing](https://learn.microsoft.com/en-us/azure/virtual-machines/states-billing)
