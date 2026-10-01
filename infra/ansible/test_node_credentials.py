"""Run actual bootstrap admission assertions locally; no credential/cloud/service calls."""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import yaml


class NodeCredentialsTests(unittest.TestCase):
    def test_public_bootstrap_and_explicit_gitops_ref_admission(self):
        play = yaml.safe_load(Path(__file__).with_name('node.yml').read_text())[0]
        tasks = copy.deepcopy(play['tasks'][:2])
        base = {'node_name':'fixture-node', 'gitops_repo':'https://github.com/example/gitops',
                'gitops_path':'clusters/aws/platform', 'gitops_revision':'main'}
        cases = [({'cloud_provider': p}, True) for p in ('aws','gcp','azure')]
        cases += [({'cloud_provider':'aws', 'gitops_token_param':'/railshot/gitops-read-token'}, True),
                  ({'cloud_provider':'gcp', 'gitops_token_param':'/railshot/gitops-read-token'}, False),
                  ({'cloud_provider':'aws', 'gitops_token_param':'/railshot/*'}, False),
                  ({'cloud_provider':'aws', 'ghcr_token_param':'/legacy/ghcr-read-token'}, False),
                  ({'cloud_provider':'aws', 'ghcr_token_param':None}, False)]
        for extra, success in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'admission.yml'
                path.write_text(yaml.safe_dump([{'hosts':'localhost', 'connection':'local', 'gather_facts':False,
                                                 'vars':{**base, **extra}, 'tasks':tasks}]))
                result = subprocess.run([shutil.which('ansible-playbook'), '-i', 'localhost,', str(path)],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'LC_ALL':'en_US.UTF-8', 'LANG':'en_US.UTF-8'})
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)

    def test_node_bootstrap_never_writes_registry_credentials(self):
        play = yaml.safe_load(Path(__file__).with_name('node.yml').read_text())[0]
        self.assertNotIn('registries.yaml', yaml.safe_dump(play))
        self.assertNotIn('ghcr.io', yaml.safe_dump(play))
        tasks = [task for task in play['tasks'] if task.get('ansible.builtin.command') == 'snap install aws-cli --classic']
        self.assertEqual(len(tasks), 1)
        self.assertIn('gitops_token_param is defined', tasks[0]['when'])


if __name__ == '__main__': unittest.main()
