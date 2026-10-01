"""Offline render checks: Terraform + PyYAML; no credentials, API, VM, or shell execution.

Run after `terraform init -backend=false`: python3 test_bootstrap.py
"""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


MODULE = Path(__file__).resolve().parent
FILES = (
    "node.yml", "files/cilium.yaml.j2", "files/traefik-config.yaml.j2",
    "files/argocd.yaml.j2", "files/root-app.yaml.j2",
)


def evaluate(expression, bundle=False, overrides=None):
    inputs = {
        "project_id": "railshot-offline-test",
        "region": "asia-northeast3", "zone": "asia-northeast3-a",
        "target_id": "gcp-offline-test", "owner_ref": "terraform:offline:gcp",
        # Syntactic fixture, not a claim that this image exists.
        "boot_image": "projects/ubuntu-os-cloud/global/images/ubuntu-2404-noble-amd64-v20000101",
        "gitops_repo": "https://github.com/example/gitops",
        "gitops_path": "clusters/gcp/platform", "gitops_revision": "main",
    }
    if bundle:
        inputs["bootstrap_files"] = {
            key: base64.b64encode(("# fixture: " + key + "\n").encode()).decode()
            for key in FILES
        }
    else:
        inputs.update(node_repo="https://github.com/example/node", node_ref="a" * 40)
    inputs.update(overrides or {})
    with tempfile.NamedTemporaryFile(mode="w", suffix=".tfvars.json") as fixture:
        json.dump(inputs, fixture)
        fixture.flush()
        result = subprocess.run(
            ["terraform", "console", "-no-color", "-var-file=" + fixture.name],
            input="jsonencode(" + expression + ")\n", text=True, capture_output=True,
            check=True, cwd=MODULE,
        )
    # Terraform 1.5 console can return status 0 despite input-validation diagnostics.
    if "Error:" in result.stderr:
        raise ValueError(result.stderr)
    return json.loads(json.loads(result.stdout))


def render(bundle=False):
    return yaml.safe_load(evaluate("local.cloud_init", bundle=bundle))


class BootstrapRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.git = render()
        cls.bundle = render(bundle=True)

    def file(self, config, path):
        return next(item for item in config["write_files"] if item["path"] == path)

    def test_both_shell_branches_parse_without_execution(self):
        for config in (self.git, self.bundle):
            script = self.file(config, "/usr/local/sbin/railshot-bootstrap")["content"]
            subprocess.run(["bash", "-n"], input=script, text=True, check=True)

    def test_git_commit_is_pinned_and_bundle_is_not_executed(self):
        script = self.file(self.git, "/usr/local/sbin/railshot-bootstrap")["content"]
        self.assertIn("--checkout '" + "a" * 40 + "'", script)
        self.assertIn("ansible-pull", script)
        self.assertNotIn("ansible-playbook", script)

    def test_bundle_files_and_execution_have_fixed_paths(self):
        paths = {item["path"] for item in self.bundle["write_files"] if item.get("encoding") == "b64"}
        self.assertEqual(paths, {"/opt/railshot/bootstrap/" + key for key in FILES})
        for item in self.bundle["write_files"]:
            if item.get("encoding") == "b64":
                self.assertTrue(base64.b64decode(item["content"]).startswith(b"# fixture:"))
        script = self.file(self.bundle, "/usr/local/sbin/railshot-bootstrap")["content"]
        self.assertIn("--connection local /opt/railshot/bootstrap/node.yml", script)
        self.assertNotIn("ansible-pull", script)

    def test_cloud_config_contains_no_aws_secret_bindings(self):
        node = yaml.safe_load(self.file(self.git, "/etc/railshot/node.yml")["content"])
        self.assertEqual(node["cloud_provider"], "gcp")
        self.assertEqual(node["gitops_path"], "clusters/gcp/platform")
        self.assertEqual(set(node), {"name", "node_name", "region", "cloud_provider", "gitops_repo", "gitops_path", "gitops_revision"})

    def test_data_mount_is_required_before_ansible(self):
        script = self.file(self.git, "/usr/local/sbin/railshot-bootstrap")["content"]
        self.assertLess(script.index("mounted_uuid="), script.index("ansible-pull"))
        self.assertIn("[ 'false' = true ]", script)
        self.assertIn("existing non-ext4 filesystem; refusing format", script)
        self.assertNotIn("mkfs.ext4 -F", script)
        unit = self.file(self.git, "/etc/systemd/system/k3s.service.d/data-disk.conf")["content"]
        self.assertIn("RequiresMountsFor=/var/lib/rancher", unit)

    def test_optional_iap_and_runtime_limit_are_off_by_default(self):
        policy = evaluate("{iap = local.iap_ssh, runtime = local.runtime_limit}")
        self.assertEqual(policy, {"iap": None, "runtime": None})

    def test_opt_in_uses_only_iap_ssh_and_stops_without_restart(self):
        policy = evaluate(
            "{iap = local.iap_ssh, runtime = local.runtime_limit}",
            overrides={"allow_iap_ssh": True, "max_run_duration_seconds": 7200},
        )
        self.assertEqual(policy["iap"]["source_ranges"], ["35.235.240.0/20"])
        self.assertEqual(policy["iap"]["ports"], ["22"])
        self.assertEqual(policy["iap"]["transport_ref"], "iap:railshot-offline-test/asia-northeast3-a/railshot-gcp")
        self.assertEqual(policy["runtime"], {
            "seconds": 7200, "automatic_restart": False, "instance_termination_action": "STOP",
        })

    def test_node_identity_is_explicit_and_existing_fqdn_can_be_preserved(self):
        config = yaml.safe_load(self.file(self.git, "/etc/railshot/node.yml")["content"])
        self.assertEqual(config["node_name"], "railshot-gcp")
        original = "railshot-gcp-poc.asia-northeast3-a.c.example.internal"
        rendered = yaml.safe_load(evaluate("local.cloud_init", overrides={"node_name": original}))
        self.assertEqual(yaml.safe_load(self.file(rendered, "/etc/railshot/node.yml")["content"])["node_name"], original)
        for invalid in ("UPPER.invalid", "node..invalid", "node/other", "a" * 64):
            with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                evaluate("local.cloud_init", overrides={"node_name": invalid})

    def test_invalid_runtime_limits_are_rejected_by_terraform(self):
        for duration in (29, 10368001, 30.5):
            with self.subTest(duration=duration):
                with self.assertRaisesRegex(ValueError, "max_run_duration_seconds must be null or an integer"):
                    evaluate("local.runtime_limit", overrides={"max_run_duration_seconds": duration})


if __name__ == "__main__":
    unittest.main(verbosity=2)
