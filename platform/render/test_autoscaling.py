"""Offline admission/render regression checks; no Kubernetes cluster is contacted."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import jsonschema
import yaml

import render


class AutoscalingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "app"
        self.spec = {"apiVersion": "jasmin/v0", "app": "memo", "services": [
            {"name": "api", "build": {"dockerfile": "Dockerfile"}, "port": 8000, "route": "/",
             "autoscaling": {"minReplicas": 1, "maxReplicas": 3, "cpu": 70, "memory": 80}}]}

    def run_render(self, **kwargs):
        return render.render(self.spec, self.out, "demo", "example.test",
                             {s["name"]: "test@sha256:" + "a" * 64 for s in self.spec["services"]},
                             "abcdef", "local-path", **kwargs)

    def admitted(self):
        return self.run_render(enable_keda=True, autoscaling_profile="bounded-v1")

    def docs(self, filename):
        return list(yaml.safe_load_all((self.out / filename).read_text()))

    def test_capability_and_admin_profile_both_required_before_writing(self):
        for options in ({}, {"enable_keda": True}, {"autoscaling_profile": "bounded-v1"},
                        {"enable_keda": True, "autoscaling_profile": "unlimited"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_render(**options)
            self.assertFalse(self.out.exists())

    def test_hpa_owns_only_opted_in_workload_and_bounded_scaler(self):
        static = copy.deepcopy(self.spec["services"][0])
        static.update(name="admin", replicas=2)
        del static["autoscaling"]
        self.spec["services"].append(static)
        self.admitted()
        scaled, fixed = self.docs("20-app.yaml")
        self.assertNotIn("replicas", scaled["spec"])
        self.assertEqual("keda", scaled["metadata"]["labels"][render.AUTOSCALING_LABEL])
        self.assertEqual(2, fixed["spec"]["replicas"])
        self.assertNotIn(render.AUTOSCALING_LABEL, fixed["metadata"]["labels"])
        self.assertNotIn(render.AUTOSCALING_LABEL, scaled["spec"]["selector"]["matchLabels"])
        obj, = self.docs("23-autoscaling.yaml")
        cfg = obj["spec"]
        self.assertEqual({"apiVersion": "apps/v1", "kind": "Deployment", "name": "memo-api"}, cfg["scaleTargetRef"])
        self.assertEqual((1, 3, 300), (cfg["minReplicaCount"], cfg["maxReplicaCount"], cfg["cooldownPeriod"]))
        self.assertEqual([{"type": "cpu", "metricType": "Utilization", "metadata": {"value": "70"}},
                          {"type": "memory", "metricType": "Utilization", "metadata": {"value": "80"}}], cfg["triggers"])
        behavior = cfg["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
        self.assertEqual(300, behavior["scaleDown"]["stabilizationWindowSeconds"])
        self.assertEqual([{"type": "Pods", "value": 1, "periodSeconds": 60}], behavior["scaleUp"]["policies"])

    def test_invalid_requests_cannot_bypass_schema_through_python_api(self):
        mutations = [lambda s: s.update(replicas=2), lambda s: s.update(strategy="canary"),
                     lambda s: s["autoscaling"].update(minReplicas=0),
                     lambda s: s["autoscaling"].update(maxReplicas=6),
                     lambda s: s["autoscaling"].update(cpu=0),
                     lambda s: s["autoscaling"].update(serverAddress="http://169.254.169.254"),
                     lambda s: s.update(autoscaling={"minReplicas": 1, "maxReplicas": 3})]
        original = copy.deepcopy(self.spec)
        for mutation in mutations:
            self.spec = copy.deepcopy(original)
            mutation(self.spec["services"][0])
            with self.subTest(spec=self.spec), self.assertRaises(jsonschema.ValidationError):
                self.admitted()
            self.assertFalse(self.out.exists())

    def test_inverted_range_and_aggregate_profile_limits_rejected(self):
        self.spec["services"][0]["autoscaling"].update(minReplicas=4, maxReplicas=3)
        with self.assertRaisesRegex(ValueError, "minReplicas"):
            self.admitted()
        self.spec["services"][0].update(size="L", autoscaling={"minReplicas": 1, "maxReplicas": 5, "cpu": 70})
        second = copy.deepcopy(self.spec["services"][0])
        second["name"] = "worker"
        self.spec["services"].append(second)
        with self.assertRaisesRegex(ValueError, "requests.cpu"):
            self.admitted()
        self.spec["services"] = [{**copy.deepcopy(second), "name": f"worker{i}", "size": "S"} for i in range(4)]
        with self.assertRaisesRegex(ValueError, "replica total"):
            self.admitted()

    def test_duplicate_service_cannot_render_two_replica_owners(self):
        duplicate = copy.deepcopy(self.spec["services"][0])
        duplicate.pop("autoscaling")
        duplicate["replicas"] = 2
        self.spec["services"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "unique"):
            self.admitted()
        self.assertFalse(self.out.exists())

    def test_quota_reserves_maximum_surge_db_and_jobs(self):
        self.spec["resources"] = {"postgres": {"size": "small"}}
        self.spec["services"][0]["migrate"] = {"command": ["python", "migrate.py"]}
        self.spec["services"].append({"name": "worker", "build": {"dockerfile": "Dockerfile"},
                                       "port": 8001, "size": "M", "replicas": 2})
        info = self.admitted()
        expected = {"pods": "12", "requests.cpu": "1650m", "requests.memory": "2176Mi",
                    "limits.cpu": "7500m", "limits.memory": "7680Mi"}
        quota = self.docs("01-guardrails.yaml")[0]["spec"]["hard"]
        self.assertEqual(expected, {k: quota[k] for k in expected})
        self.assertEqual(expected, info["autoscaling"]["quota"])
        self.out = self.out.parent / "larger-release"
        self.spec["services"][0]["autoscaling"]["maxReplicas"] = 5
        self.admitted()
        new = self.docs("01-guardrails.yaml")[0]["spec"]["hard"]
        self.assertEqual("14", new["pods"])
        self.assertEqual("1850m", new["requests.cpu"])

    def test_opt_out_requires_fresh_artifact_and_restores_static_replica(self):
        self.admitted()
        del self.spec["services"][0]["autoscaling"]
        with self.assertRaisesRegex(ValueError, "empty directory"):
            self.run_render()
        self.out = self.out.parent / "next-release"
        info = self.run_render()
        self.assertEqual([], self.docs("23-autoscaling.yaml"))
        self.assertEqual(1, self.docs("20-app.yaml")[0]["spec"]["replicas"])
        self.assertNotIn("autoscaling", info)

    def test_argocd_replica_exception_is_label_targeted(self):
        path = render.PLATFORM.parent / "gitops-template/clusters/aws/platform/20-tenants.yaml"
        project, appset = yaml.safe_load_all(path.read_text())
        self.assertIn({"group": "keda.sh", "kind": "ScaledObject"}, project["spec"]["namespaceResourceWhitelist"])
        app = appset["spec"]["template"]["spec"]
        self.assertIn("RespectIgnoreDifferences=true", app["syncPolicy"]["syncOptions"])
        rule, = app["ignoreDifferences"]
        self.assertEqual(("apps", "Deployment"), (rule["group"], rule["kind"]))
        expr, = rule["jqPathExpressions"]
        # Exercise the actual configured JQ on both replica owners, not a duplicate Python predicate.
        for labels, expected in (({render.AUTOSCALING_LABEL: "keda"}, "7"), ({}, "")):
            result = subprocess.run(["jq", "-c", expr], input=json.dumps({"metadata": {"labels": labels}, "spec": {"replicas": 7}}),
                                    text=True, capture_output=True, check=True)
            self.assertEqual(expected, result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
