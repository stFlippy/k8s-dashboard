import unittest

from quaber.views import MatrixViews


class ViewTests(unittest.TestCase):
    def test_web_format_and_cluster_conflicts(self):
        preferences = {}
        views = MatrixViews(preferences)
        view = views.add("Staging")
        view["selectedClusterIds"] = ["cluster-1"]
        payload = views.export([{"id": "cluster-1", "name": "One"}])
        self.assertEqual(payload["format"], "quaber-matrix-views")
        self.assertEqual(payload["requiredClusters"], [{"id": "cluster-1", "name": "One"}])
        with self.assertRaisesRegex(ValueError, "Нет кластеров"):
            MatrixViews({}).import_data(payload, [])
        imported = MatrixViews({"views": [{"id": "other", "name": "Other"}]})
        self.assertEqual(imported.import_data(payload, [{"id": "cluster-1", "name": "One"}]), 2)
        self.assertEqual(len(imported.views), 3)
        with self.assertRaisesRegex(ValueError, "повторяющиеся"):
            imported.import_data(payload, [{"id": "cluster-1", "name": "One"}])


if __name__ == "__main__":
    unittest.main()
