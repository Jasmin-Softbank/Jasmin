# AWS private CI workspace

This module bootstraps the **same** `infra/ansible/ci.yml` and `platform/control/worker.py` used on the GCP CI VM. It adds provider resources and transport; it does not create a second CI implementation or a new app cluster. One `t3.xlarge` (configured 4 vCPU/16 GiB, Standard CPU credits), encrypted retained 60 GiB gp3 root disk, no inbound security-group rules, IMDSv2 with hop limit one. The public address is for outbound package/SSM access. HTTP/HTTPS and the two configured public DNS resolvers are allowed outbound; container isolation remains the common native probe's responsibility.

The instance role has SSM agent permissions with an explicit deny for all Parameter Store and Secrets Manager credential reads. No model/GitHub/registry key or cloud management permission is installed. The SSH public key belongs to the private administrator, who connects through authenticated SSM; SSH is not exposed on the public interface. The administrator has sudo and this VM is not a multi-tenant host boundary. Uploaded app checks execute under the common container policy. GitHub self-hosted runner registration is not provided by this module.

Required variables: registered `account_id`, exact Canonical Ubuntu 24.04 amd64 `ami_id`, published `platform_ref`, SHA256 of `https://codeload.github.com/Jasmin-Softbank/Jasmin/tar.gz/<platform_ref>`, administrator Ed25519 **public** key without comment, and UTC `stop_at`. No private key is copied to the VM or Terraform state. Bootstrap arms an absolute systemd STOP timer before network installs and refuses already-expired reuse. This guest timer is a bounded PoC cutoff, not cloud-enforced orchestration or job draining. Stopping retains disk data and disk cost. Review and set a new deadline before intentional reuse.

```sh
terraform -chdir=infra/terraform/ci init -backend=false
terraform -chdir=infra/terraform/ci validate
terraform -chdir=infra/terraform/ci plan -state=/private/path/ci.tfstate -var-file=/private/path/ci.tfvars.json -out=/private/path/ci.tfplan
terraform -chdir=infra/terraform/ci show /private/path/ci.tfplan
# Apply only the reviewed plan, using independent private state/backend.
terraform -chdir=infra/terraform/ci apply -state=/private/path/ci.tfstate /private/path/ci.tfplan
terraform -chdir=infra/terraform/ci output -state=/private/path/ci.tfstate -json node_descriptor
```

The local operator needs AWS CLI credentials authorized for that instance, the Session Manager plugin, OpenSSH and the corresponding local private key. Configure the standard `AWS-StartSSHSession` proxy, then invoke the unchanged worker argv over SSH: `sudo -n /usr/bin/python3 /opt/railshot/source-<commit>/platform/control/worker.py --root /var/lib/railshot-console`. Put that fixed SSH prefix/worker argv into the existing control-server configuration; uploaded JSON never chooses the command or root. [AWS SSH-over-SSM prerequisites and ProxyCommand](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-getting-started-enable-ssh-connections.html).

Readiness must be measured with the real `inspect` operation after cloud-init completes: `ci_ready`, native network proof and builder proof all true. The common `railshot-ci-verify.service` reruns actual isolation checks after Docker/network restart. Terraform `UNVERIFIED` output, a boot marker or SSM Online alone is not CI success. Local checks: `python3 -m unittest discover -s infra/terraform/ci -p test_bootstrap.py`. AWS VM creation, SSM transport and full CI remain unverified until their native receipts exist.
