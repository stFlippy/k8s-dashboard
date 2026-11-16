"""Owner-chain and empty-state regressions for the heterogeneous matrix."""
import unittest
from quaber.resource_index import index_scope, empty_workload
from quaber.kubernetes import Kubernetes


def resource(kind, name, uid, owner=None):
    value = {"kind": kind, "metadata": {"name": name, "uid": uid}, "spec": {}}
    if owner:
        value["metadata"]["ownerReferences"] = [dict(owner, controller=True)]
    return value


class WorkloadTests(unittest.TestCase):
    def test_pods_resolve_to_roots_without_duplicate_jobs_or_replica_sets(self):
        dep = resource("Deployment", "app", "d")
        rs = resource("ReplicaSet", "app-rs", "rs", {"kind": "Deployment", "name": "app", "uid": "d"})
        cron = resource("CronJob", "backup", "cron")
        job = resource("Job", "backup-run", "job", {"kind": "CronJob", "name": "backup", "uid": "cron"})
        stateful = resource("StatefulSet", "db", "ss")
        daemon = resource("DaemonSet", "agent", "ds")
        pods = [resource("Pod", "app-pod", "p1", {"kind": "ReplicaSet", "name": "app-rs", "uid": "rs"}),
                resource("Pod", "backup-pod", "p2", {"kind": "Job", "name": "backup-run", "uid": "job"}),
                resource("Pod", "db-0", "p3", {"kind": "StatefulSet", "name": "db", "uid": "ss"}),
                resource("Pod", "agent-node", "p4", {"kind": "DaemonSet", "name": "agent", "uid": "ds"}),
                resource("Pod", "standalone", "p5")]
        entries, index = index_scope({"deployments": [dep], "replica_sets": [rs], "jobs": [job],
                "cron_jobs": [cron], "stateful_sets": [stateful], "daemon_sets": [daemon], "pods": pods})
        self.assertEqual(set(entries), {"app", "CronJob/backup", "StatefulSet/db", "DaemonSet/agent", "Pod/standalone"})
        self.assertEqual(sum(map(len, index.values())), len(pods))
        self.assertEqual(index["cron"][0]["metadata"]["name"], "backup-pod")
        self.assertEqual(index["d"][0]["metadata"]["name"], "app-pod")

    def test_owner_name_comes_from_reference_not_pod_prefix(self):
        job = resource("Job", "unrelated-job-name", "job", {"kind": "CronJob", "name": "real-schedule", "uid": "cron"})
        pod = resource("Pod", "arbitrary", "p", {"kind": "Job", "name": "unrelated-job-name", "uid": "job"})
        entries, index = index_scope({"jobs": [job], "pods": [pod]})
        self.assertIn("CronJob/real-schedule", entries)
        self.assertTrue(entries["CronJob/real-schedule"]["_unresolved"])
        self.assertEqual(index["cron"], [pod])

    def test_unknown_controller_and_standalone_job_are_not_dropped(self):
        job = resource("Job", "manual", "job")
        pod = resource("Pod", "custom", "p", {"kind": "CustomWorkload", "name": "custom-owner", "uid": "custom"})
        entries, index = index_scope({"jobs": [job], "pods": [pod]})
        self.assertIn("Job/manual", entries)
        self.assertIn("CustomWorkload/custom-owner", entries)
        self.assertEqual(index["custom"], [pod])

    def test_mirror_pod_has_its_own_row_not_a_synthetic_node_workload(self):
        pod = resource("Pod", "kube-apiserver-node", "mirror", {"kind": "Node", "name": "node", "uid": "node"})
        entries, index = index_scope({"pods": [pod]})
        self.assertEqual(set(entries), {"Pod/kube-apiserver-node"})
        self.assertEqual(index["mirror"], [pod])

    def test_idle_cronjob_and_completed_job_are_not_failed_deployments(self):
        cron = resource("CronJob", "nightly", "cron")
        text, alarm = empty_workload(cron, [])
        self.assertEqual(text, "Ожидание запуска")
        self.assertFalse(alarm)
        cron["spec"]["suspend"] = True
        self.assertEqual(empty_workload(cron, [])[0], "Приостановлен")
        job = resource("Job", "done", "job")
        job["status"] = {"conditions": [{"type": "Complete", "status": "True"}]}
        self.assertEqual(empty_workload(job, []), ("Завершён", False))

    def test_paths_use_current_apps_and_batch_groups(self):
        self.assertEqual(Kubernetes.path("cronjob", "ns", "schedule"), "/apis/batch/v1/namespaces/ns/cronjobs/schedule")
        self.assertEqual(Kubernetes.path("statefulset", "ns"), "/apis/apps/v1/namespaces/ns/statefulsets")
