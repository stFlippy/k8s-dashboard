import unittest

from quaber.views import MatrixViews


class ViewTests(unittest.TestCase):
    def test_legacy_empty_namespace_setting_is_ignored_and_removed_without_losing_views(self):
        for legacy in (True,False,{'obsolete':True}):
            with self.subTest(legacy=legacy):
                raw = dict(id='saved',name='Saved',selectedNamespaces=['busy'],
                           namespaceOrder=['a/busy'],hideFilteredNamespaces=legacy)
                preferences = dict(views=[raw],active_view='saved')
                views = MatrixViews(preferences)
                self.assertEqual(views.active['name'],'Saved')
                self.assertEqual(views.active['selectedNamespaces'],['busy'])
                self.assertEqual(views.active['namespaceOrder'],['a/busy'])
                self.assertNotIn('hideFilteredNamespaces',views.active)
                views.persist()
                self.assertNotIn('hideFilteredNamespaces',preferences['views'][0])
                exported = views.export([dict(id='a',name='Lab')])
                self.assertNotIn('hideFilteredNamespaces',exported['views'][0])
                exported['views'][0]['hideFilteredNamespaces'] = legacy
                imported = MatrixViews({})
                imported.import_data(exported,[dict(id='a',name='Lab')])
                self.assertEqual(imported.active['name'],'Saved')
                self.assertEqual(imported.active['selectedNamespaces'],['busy'])
                self.assertNotIn('hideFilteredNamespaces',imported.active)

    def test_new_view_is_empty_independent_and_stays_empty_after_discovery(self):
        views = MatrixViews({})
        first = views.active
        first.update(selectedClusterIds=['a'], selectedNamespaces=['busy'],
                     namespaceOrder=['a/busy'], color='#112233')
        first['filters']['Running'] = False
        views.observe_scopes({'ClusterIds': {'a'}, 'Namespaces': {'busy'}, 'Deployments': {'app'}})
        empty = views.add('Empty')
        self.assertEqual(empty['selectedClusterIds'], [])
        self.assertIsNone(empty['selectedNamespaces'])
        self.assertIsNone(empty['selectedDeployments'])
        self.assertEqual(empty['namespaceOrder'], [])
        self.assertNotIn('color', empty)
        self.assertTrue(empty['filters']['Running'])
        self.assertNotIn('hideFilteredNamespaces',empty)
        views.observe_scopes({'ClusterIds': {'a', 'b'}, 'Namespaces': {'busy', 'new'}, 'Deployments': {'app', 'new'}})
        views.reconcile_connections({'a','b'})
        self.assertEqual(empty['selectedClusterIds'], [])
        restored = MatrixViews(views.preferences)
        self.assertEqual(restored.active['selectedClusterIds'], [])
        self.assertFalse(first['filters']['Running'])

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
