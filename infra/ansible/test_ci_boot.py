"""No runtime success inferred: verify the boot dependency and real probe wiring."""
from pathlib import Path
import unittest
import yaml


class CIBootTests(unittest.TestCase):
    def test_boot_acceptance_waits_for_policy_and_runs_real_checks(self):
        tasks = yaml.safe_load(Path(__file__).with_name('ci.yml').read_text())[0]['tasks']
        unit = next(t['ansible.builtin.copy']['content'] for t in tasks
                    if t.get('ansible.builtin.copy', {}).get('dest') == '/etc/systemd/system/railshot-ci-verify.service')
        for required in ('Requires=docker.service railshot-ci-network.service',
                         'After=docker.service railshot-ci-network.service network-online.target',
                         'PartOf=docker.service railshot-ci-network.service',
                         'WantedBy=railshot-ci-network.service', 'ExecStart=/usr/local/sbin/test-ci-network',
                         'TimeoutStartSec=600'):
            self.assertIn(required, unit)
        self.assertNotIn('network-verified.sha256', unit)
        builder = next(i for i,t in enumerate(tasks) if t['name'].startswith('Prepare the dedicated bounded BuildKit'))
        start = next(i for i,t in enumerate(tasks) if t.get('ansible.builtin.systemd_service', {}).get('name') == 'railshot-ci-verify')
        self.assertGreater(start, builder)
        self.assertEqual(tasks[start]['ansible.builtin.systemd_service']['state'], 'restarted')
        self.assertTrue(tasks[start]['ansible.builtin.systemd_service']['enabled'])


if __name__ == '__main__': unittest.main()
