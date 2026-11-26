"""Real local TLS: bearer/certificate, trust rejection and unchanged proxy default."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor
from quaber.storage import UserStore
from quaber.kubernetes import Kubernetes
from quaber.cluster_workers import configure_cluster_threads, configured_threads
from quaber.views import validate_preferences
from https_fixture import TLSEndpoint


class HTTPSTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = UserStore(Path(self.folder.name)/'UserData')

    def api(self, endpoint, **options):
        record = self.store.put_cluster('Fixture', endpoint.server_url, 'fixture', endpoint.config(**options))
        proxy = Mock()
        authorization = SimpleNamespace(renew=Mock(side_effect=RuntimeError('No fixture login')), block=Mock())
        api = Kubernetes(self.store, proxy, authorization=authorization)
        self.addCleanup(api.close)
        api.set_transport_mode('https')
        return api, record, proxy

    def endpoint(self, **options):
        endpoint = TLSEndpoint(self.folder.name, **options)
        self.addCleanup(endpoint.close)
        return endpoint

    def test_bearer_https_lists_namespaces_without_starting_kubectl_proxy(self):
        endpoint = self.endpoint()
        api, record, proxy = self.api(endpoint)
        with ThreadPoolExecutor(max_workers=1) as pool:
            names = pool.submit(api.namespaces, record['id']).result(timeout=4)
        self.assertEqual(len(names), 10)
        self.assertEqual(api.request_policy.stats['attempts'], 1)
        self.assertEqual(api.request_policy.statuses, {200:1})
        proxy.url.assert_not_called()

    def test_untrusted_certificate_fails_without_proxy_fallback(self):
        endpoint = self.endpoint()
        api, record, proxy = self.api(endpoint, trust=False)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(Exception): pool.submit(api.namespaces, record['id']).result(timeout=4)
        self.assertEqual(endpoint.calls, [])
        proxy.url.assert_not_called()

    def test_client_certificate_is_supported(self):
        endpoint = self.endpoint(require_client_cert=True)
        api, record, proxy = self.api(endpoint, client_cert=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            names = pool.submit(api.namespaces, record['id']).result(timeout=4)
        self.assertEqual(len(names), 10)
        proxy.url.assert_not_called()

    def test_redirect_is_not_followed(self):
        endpoint = self.endpoint()
        endpoint.redirect = endpoint.server_url+'/redirect-target'
        api, record, _ = self.api(endpoint)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(RuntimeError): pool.submit(api.namespaces, record['id']).result(timeout=4)
        self.assertEqual(len(endpoint.calls), 1)

    def test_https_cannot_dispatch_resource_mutation_without_a_permit(self):
        endpoint = self.endpoint()
        api, record, proxy = self.api(endpoint)
        for method in ('PUT','PATCH','POST','DELETE'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with self.assertRaises(PermissionError):
                    pool.submit(api.request, record['id'], '/api/v1/namespaces/ns-01/configmaps/test', method, {}).result(timeout=3)
        self.assertEqual(endpoint.calls, [])
        proxy.url.assert_not_called()

    def test_proxy_is_default_and_preferences_accept_zero_through_fifty(self):
        self.assertEqual(validate_preferences({})['api_transport'], 'proxy')
        api = Kubernetes(self.store, Mock())
        self.addCleanup(api.close)
        self.assertEqual(api.transport_mode, 'proxy')
        for count in range(51):
            self.assertEqual(validate_preferences({'cluster_io_threads':count})['cluster_io_threads'],count)
            configure_cluster_threads(api,count)
            self.assertEqual(configured_threads(api),count)
            self.assertEqual(api.request_policy.cluster_concurrency,max(1,count))

    def test_zero_runs_serial_reads_in_collector_without_creating_an_io_pool(self):
        import threading
        from quaber.cluster_workers import ClusterWorkers
        workers = ClusterWorkers(0)
        self.addCleanup(workers.close)
        with ThreadPoolExecutor(max_workers=1) as pool:
            def execute(): return threading.get_ident(), workers.run('c', threading.get_ident)
            caller, network = pool.submit(execute).result(timeout=2)
        self.assertEqual(caller,network)
        self.assertIsNone(workers.pools['c']['pool'])
