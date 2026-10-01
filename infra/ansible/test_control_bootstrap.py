#!/usr/bin/env python3
"""Exercise the actual embedded importer with fake SSM data, never local credentials.

Run: python3 infra/ansible/test_control_bootstrap.py
Ansible --syntax-check separately validates the surrounding playbook YAML.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch


class ClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "codex"
        self.home.mkdir(mode=0o700)
        self.target = self.home / "auth.json"
        self.raw = json.dumps({"auth_mode": "chatgpt", "tokens": {
            "access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}})
        text = Path(__file__).with_name("control.yml").read_text()
        match = re.search(
            r"(?ms)^    - name: Install one-parameter credential importer\n.*?"
            r"^        content: \|\n(.*?)(?=^    - name:|\Z)", text)
        self.assertIsNotNone(match)
        self.code = textwrap.dedent(match.group(1)).replace("{{ codex_home }}", str(self.home))
        self.code = self.code.replace("{{ railshot_state_dir }}", str(self.root))
        self.code = self.code.replace("{{ railshot_region }}", "ap-northeast-2")

    def run_import(self, value=None, error=None, parameter_type="SecureString", replace=None):
        calls = []
        def get_parameter(**kwargs):
            calls.append(kwargs)
            if error:
                raise ClientError(error)
            return {"Parameter": {"Value": self.raw if value is None else value, "Type": parameter_type}}
        client = types.SimpleNamespace(get_parameter=get_parameter)
        modules = {
            "boto3": types.SimpleNamespace(client=lambda *a, **k: client),
            "botocore": types.ModuleType("botocore"),
            "botocore.exceptions": types.SimpleNamespace(ClientError=ClientError),
        }
        owner = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        output = io.StringIO()
        result = 0
        with patch.dict("sys.modules", modules), patch("pwd.getpwnam", return_value=owner), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with patch("os.replace", side_effect=replace or os.replace):
                try:
                    exec(compile(self.code, "embedded-auth-importer", "exec"), {})
                except SystemExit as exc:
                    result = exc.code
        self.assertEqual(output.getvalue(), "", "Importer must never log credential material")
        if calls:
            self.assertEqual(calls, [{"Name": "/railshot/codex/operator-primary/auth", "WithDecryption": True}])
        self.assertEqual(list(self.home.glob(".auth-*")), [], "No intermediate credential files may remain")
        state = json.loads((self.root / "codex-auth-status.json").read_text())
        self.assertFalse(state["authenticated_call_verified"])
        return result, state["state"], calls

    def test_valid_import_is_private_and_atomic(self):
        actual_replace = os.replace
        def checked_replace(source, dest):
            self.assertFalse(Path(dest).exists(), "Target must appear only after a full write")
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            self.assertEqual(Path(source).read_text(), self.raw)
            actual_replace(source, dest)
        rc, state, _ = self.run_import(replace=checked_replace)
        self.assertEqual((rc, state), (0, "imported_unverified"))
        self.assertEqual(self.target.read_text(), self.raw)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_invalid_auth_is_not_written(self):
        values = [{"auth_mode": "apikey", "tokens": {"access_token": "x", "refresh_token": "y"}},
                  {"auth_mode": "chatgpt", "tokens": {"access_token": "x"}},
                  {"auth_mode": "chatgpt", "tokens": {"access_token": "", "refresh_token": "y"}},
                  {"auth_mode": "chatgpt", "tokens": {"access_token": "x", "refresh_token": ""}}]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(self.run_import(json.dumps(value))[:2], (1, "credential_import_failed"))
                self.assertFalse(self.target.exists())
        self.assertEqual(self.run_import(parameter_type="String")[:2], (1, "credential_import_failed"))

    def test_missing_parameter_is_pending_but_access_denied_fails(self):
        self.assertEqual(self.run_import(error="ParameterNotFound")[:2], (0, "pending_parameter"))
        self.assertEqual(self.run_import(error="AccessDeniedException")[:2], (1, "parameter_read_failed"))
        self.assertFalse(self.target.exists())

    def test_interrupted_atomic_write_fails_and_cleans_up(self):
        def fail_replace(*args):
            raise OSError("synthetic interruption")
        self.assertEqual(self.run_import(replace=fail_replace)[:2], (1, "credential_import_failed"))
        self.assertFalse(self.target.exists())

    def test_local_refreshed_auth_is_preserved_and_secured(self):
        self.target.write_text("synthetic-local-refreshed-auth")
        self.target.chmod(0o644)
        self.assertEqual(self.run_import(error="AccessDeniedException"),
                         (0, "local_auth_preserved_unverified", []))
        self.assertEqual(self.target.read_text(), "synthetic-local-refreshed-auth")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_symlink_is_rejected(self):
        self.target.symlink_to(self.root / "outside")
        self.assertEqual(self.run_import()[:2], (1, "credential_import_failed"))


if __name__ == "__main__":
    unittest.main()
