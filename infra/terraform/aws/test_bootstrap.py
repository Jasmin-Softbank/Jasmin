"""Offline Terraform template rendering and fail-closed mount contract. No cloud or formatting."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import yaml

MODULE = Path(__file__).resolve().parent


def evaluate(expression):
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(['terraform', 'console', '-no-color'], cwd=directory,
                                input='jsonencode(' + expression + ')\n', capture_output=True, text=True, check=True)
    if 'Error:' in result.stderr:
        raise ValueError(result.stderr)
    return json.loads(json.loads(result.stdout))


def render():
    args = {'device': '/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol01234567890123456',
            'initialize_empty_data_disk': 'false', 'node_repo': 'https://github.com/example/bootstrap', 'node_ref': 'a' * 40}
    script = evaluate('templatefile(' + json.dumps(str(MODULE / 'bootstrap.sh.tftpl')) + ',' + json.dumps(args) + ')')
    config = {'name': 'offline', 'cloud_provider': 'aws', 'region': 'ap-northeast-2',
              'gitops_repo': 'https://github.com/example/gitops', 'gitops_path': 'clusters/aws/platform', 'gitops_revision': 'main'}
    data = {'node_config': yaml.safe_dump(config), 'bootstrap_script': script,
            'bootstrap_manifest': json.dumps({'method': 'pinned-public-git', 'revision': 'a' * 40})}
    return yaml.safe_load(evaluate('templatefile(' + json.dumps(str(MODULE / 'cloud-init.yaml.tftpl')) + ',' + json.dumps(data) + ')'))


class AWSBootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = render()
        cls.files = {x['path']: x for x in cls.config['write_files']}
        cls.script = cls.files['/usr/local/sbin/railshot-bootstrap']['content']

    def test_pinned_commit_and_node_config_are_rendered(self):
        node = yaml.safe_load(self.files['/etc/railshot/node.yml']['content'])
        self.assertEqual(node['cloud_provider'], 'aws')
        self.assertEqual(node['gitops_path'], 'clusters/aws/platform')
        self.assertIn("--checkout '" + 'a' * 40 + "'", self.script)
        subprocess.run(['bash', '-n'], input=self.script, text=True, check=True)

    def test_disk_identity_and_fail_closed_mount_precede_ansible(self):
        self.assertIn('nvme-Amazon_Elastic_Block_Store_vol01234567890123456', self.script)
        self.assertLess(self.script.index('mounted_uuid='), self.script.index('ansible-pull'))
        for guard in ('required data device missing', 'partitioned disk rejected',
                      'empty disk initialization not authorized', 'unknown disk signature',
                      'existing non-ext4 filesystem', 'refusing to cover existing local node data', 'wrong data disk mounted'):
            self.assertIn(guard, self.script)
        self.assertNotIn('mkfs.ext4 -F', self.script)
        self.assertNotIn('nofail', self.script)
        self.assertIn('RequiresMountsFor=/var/lib/rancher', self.files['/etc/systemd/system/k3s.service.d/data-disk.conf']['content'])

    def test_runtime_does_not_invent_mount_or_bootstrap_readiness(self):
        self.assertIn('cluster_ready":"unverified', self.script)
        self.assertEqual(self.config['runcmd'], [['bash', '/usr/local/sbin/railshot-bootstrap']])

    def test_native_variable_validation_rejects_floating_sources(self):
        values = {'target_id': 'aws-offline', 'owner_ref': 'terraform:offline:test',
                  'account_id': '000000000000', 'ami_id': 'ami-' + '0' * 17, 'node_ref': 'a' * 40,
                  'gitops_repo': 'https://github.com/example/gitops', 'gitops_path': 'clusters/aws/platform', 'gitops_revision': 'main'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'variables.tf').write_text((MODULE / 'variables.tf').read_text())
            for key, bad in (('node_ref', 'main'), ('ami_id', 'stable/current'), ('instance_type', 'unreviewed-size')):
                with self.subTest(key=key):
                    inputs = root / 'inputs.tfvars.json'; inputs.write_text(json.dumps({**values, key: bad}))
                    result = subprocess.run(['terraform', 'console', '-no-color', '-var-file=' + str(inputs)], cwd=root,
                                            input='jsonencode(var.' + key + ')\n', capture_output=True, text=True)
                    self.assertIn('Error:', result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
