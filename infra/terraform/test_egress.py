"""Native rule/source contract plus real temporary apt-source rewrite; no cloud/network."""
import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


def module_at(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class HostEgressTest(unittest.TestCase):
    def test_gcp_replaces_implied_public_egress_with_https_and_deny(self):
        source = (ROOT / 'gcp/main.tf').read_text()
        self.assertRegex(source, r'resource "google_compute_firewall" "host_https"')
        self.assertRegex(source, r'https_ports\s*=\s*\["443"\]')
        self.assertRegex(source, r'ports\s*=\s*local.host_egress.https_ports')
        deny = source.split('resource "google_compute_firewall" "host_deny_other"', 1)[1]
        self.assertRegex(deny, r'direction\s*=\s*"EGRESS"')
        self.assertRegex(deny, r'priority\s*=\s*2000')
        self.assertRegex(deny, r'deny\s*\{\s*protocol\s*=\s*"all"')
        self.assertIn('target_service_accounts', deny)
        self.assertIn('google-platform-always-allowed', source)

    def test_azure_overrides_vnet_and_internet_defaults_without_ssh_egress(self):
        source = (ROOT / 'azure/main.tf').read_text()
        self.assertIn('for_each = local.host_egress_rules', source)
        self.assertRegex(source, r'name\s*=\s*"deny-other-outbound"')
        self.assertRegex(source, r'priority\s*=\s*4096')
        self.assertRegex(source, r'direction\s*=\s*"Outbound"')
        rules = source.split('host_egress_rules = {', 1)[1]
        for name in ('https', 'dns_udp', 'dns_tcp', 'ntp', 'agent'):
            self.assertRegex(rules, name + r'\s*=')
        self.assertNotIn('"22"', rules)
        self.assertNotIn('"VirtualNetwork"', rules)
        self.assertIn('"AzurePlatformDNS"', rules)
        self.assertIn('"168.63.129.16/32"', rules)

    def test_both_cloud_init_profiles_rewrite_ubuntu_apt_to_https_before_updates(self):
        # Exercise the real Python payload against temporary paths only.
        for provider in ('gcp', 'azure'):
            source = (ROOT / provider / 'cloud-init.yaml.tftpl').read_text()
            self.assertIn('preserve_sources_list: true', source)
            payload = source.split("<<'PYBOOT'\n", 1)[1].split('    PYBOOT', 1)[0]
            payload = '\n'.join(line[4:] for line in payload.splitlines())
            with tempfile.TemporaryDirectory() as directory:
                apt = Path(directory) / 'apt'; (apt / 'sources.list.d').mkdir(parents=True)
                path = apt / 'sources.list.d/ubuntu.sources'
                path.write_text('URIs: http://region.cloud.archive.ubuntu.com/ubuntu\n')
                script = payload.replace('/etc/apt', str(apt))
                result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(path.read_text(), 'URIs: https://archive.ubuntu.com/ubuntu\n')
                path.write_text('URIs: http://unreviewed.example/repository\n')
                failed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn('bootstrap blocked', failed.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
