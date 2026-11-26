"""Account migration, serial physical I/O and aggregate namespace filtering."""
import copy
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PyQt5.QtWidgets import QApplication
from quaber.storage import UserStore
from quaber.request_policy import RequestPolicy, LIMITS
from quaber.collection_reader import CollectionReader
from quaber.auth_queue import serialized_login
from test_storage import CONFIG
from test_v3_requests import fake_api


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = UserStore(Path(self.folder.name))

    def test_legacy_shared_accounts_migrate_atomically_without_duplicate_secrets(self):
        records = []
        for index, secret in enumerate(('obsolete', 'current')):
            records.append(dict(id=str(index), name=str(index), server='https://example.test:6443',
                username='alice', sharedPassword=True, enabled=True, authMode='login',
                encryptedPassword=self.store.encrypt(secret), encryptedKubeconfig=self.store.encrypt(CONFIG),
                createdAt='2026-01-01', updatedAt=f'2026-01-0{index+1}'))
        self.store._write_json(self.store.clusters_file, dict(version=1, clusters=records))
        migrated = self.store.clusters()
        self.assertEqual(len(self.store.accounts()), 1)
        self.assertEqual(len({c['accountId'] for c in migrated}), 1)
        self.assertEqual([self.store.saved_password(c) for c in migrated], ['current', 'current'])
        raw = json.loads(self.store.clusters_file.read_text(encoding='utf-8'))
        self.assertTrue(all('encryptedPassword' not in c and 'sharedPassword' not in c for c in raw['clusters']))
        self.assertNotIn('current', self.store.clusters_file.read_text(encoding='utf-8'))
        self.assertEqual(UserStore(self.store.root).clusters(), migrated)

    def test_account_bindings_and_password_edits_are_shared_but_tokens_stay_per_cluster(self):
        account = self.store.put_account('Production', 'alice', 'first', remember_password=True)
        records = [self.store.put_cluster(str(i), 'https://example.test:6443', 'alice', CONFIG,
                   account_id=account['id']) for i in range(2)]
        configs = [c['encryptedKubeconfig'] for c in records]
        self.store.put_account('Production', 'alice', 'second', account_id=account['id'], remember_password=True)
        current = self.store.clusters()
        self.assertEqual([self.store.saved_password(c) for c in current], ['second', 'second'])
        self.assertEqual([c['encryptedKubeconfig'] for c in current], configs)
        self.assertNotEqual(current[0]['accountRevision'], records[0]['accountRevision'])
        with self.assertRaises(ValueError): self.store.delete_account(account['id'])
        self.store.put_account('Production', 'bob', account_id=account['id'])
        self.assertTrue(all(not c['enabled'] and not c['encryptedKubeconfig'] for c in self.store.clusters()))

    def test_session_only_secret_is_lost_at_restart(self):
        account = self.store.put_account('Test', 'alice', 'session-only', remember_password=False)
        self.assertEqual(self.store.account_password(account['id']), 'session-only')
        self.assertEqual(UserStore(self.store.root).account_password(account['id']), '')
        self.assertNotIn('session-only', self.store.clusters_file.read_text(encoding='utf-8'))

    def test_bad_account_causes_only_one_login_attempt_across_six_clusters(self):
        account = dict(accountId='account', username='alice')
        threads = []
        def reject():
            threads.append(threading.get_ident())
            raise RuntimeError('invalid credentials')
        def attempt(_):
            try: serialized_login(self.store, account, 'wrong', reject)
            except Exception: pass
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(attempt, range(6)))
        self.assertEqual(len(threads), 1)
        result = serialized_login(self.store, dict(accountId='other', username='bob'), 'correct',
                                  lambda: threading.get_ident())
        self.assertEqual(result, threads[0])

    def test_existing_session_password_can_be_saved_without_retyping(self):
        account = self.store.put_account('Test', 'alice', 'session-secret', remember_password=False)
        self.store.put_account('Test', 'alice', account_id=account['id'], remember_password=True)
        self.assertEqual(UserStore(self.store.root).account_password(account['id']), 'session-secret')


class LoadingTests(unittest.TestCase):
    def test_one_physical_thread_per_cluster_with_parallel_clusters(self):
        api, _, _ = fake_api(cluster_count=3)
        self.addCleanup(api.close)
        api.request_policy = RequestPolicy(limits=replace(LIMITS, cluster_rate=1000, cluster_burst=100))
        lock = threading.Lock()
        active, peaks, identities = {}, {}, {}
        barrier = threading.Barrier(3)
        first = set()
        def route(request):
            cid = request.full_url.split('/')[2]
            with lock:
                active[cid] = active.get(cid, 0)+1
                peaks[cid] = max(peaks.get(cid, 0), active[cid])
                identities.setdefault(cid, set()).add(threading.get_ident())
                initial = cid not in first
                first.add(cid)
            if initial: barrier.wait(timeout=3)
            time.sleep(.01)
            with lock: active[cid] -= 1
            return 200, {}, {'items': []}
        api.opener.route = route
        with ThreadPoolExecutor(max_workers=9) as pool:
            futures = [pool.submit(api.request, f'c{i%3}', '/api/v1/pods') for i in range(9)]
            for future in futures: future.result(timeout=5)
        self.assertEqual(list(peaks.values()), [1, 1, 1])
        self.assertTrue(all(len(ids) == 1 for ids in identities.values()))
        self.assertEqual(len(set.union(*identities.values())), 3)

    def test_foreign_namespace_is_never_queried_or_invented(self):
        api, _, _ = fake_api(namespaces=2, cluster_count=1)
        self.addCleanup(api.close)
        reader = CollectionReader(api)
        plan = reader.plan('c0', ['ns0', 'only-in-other-cluster'])
        result = reader.read('c0', 'deployment', plan)
        self.assertEqual(set(result.groups), {'ns0'})
        self.assertFalse(any('/namespaces/' in c['path'] for c in api.opener.calls))
        self.assertEqual(len(api.opener.calls), 2)  # Namespace LIST + aggregate Deployment LIST.

    def test_403_details_reach_both_gui_and_file_without_secret(self):
        from quaber.app_logging import LogSession, register_secret
        app = QApplication.instance() or QApplication([])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = folder.name
        session = LogSession(Path(root))
        messages = []
        session.bridge.message.connect(messages.append)
        self.addCleanup(session.close)
        register_secret('fixture-secret')
        api, _, _ = fake_api(cluster_count=1)
        self.addCleanup(api.close)
        api.opener.route = lambda _: (403, {}, dict(message='deployments.apps forbidden fixture-secret'))
        with self.assertRaises(PermissionError): api.request('c0', '/apis/apps/v1/deployments')
        app.processEvents()
        text = '\n'.join(messages)
        self.assertIn('deployments.apps forbidden', text)
        self.assertNotIn('fixture-secret', text)
        self.assertIn('deployments.apps forbidden', (Path(root)/'logs/quaber.log').read_text(encoding='utf-8'))

    def test_cron_owner_match_requires_uid_and_controller(self):
        from quaber.cron_history import owned_by
        pod = dict(metadata=dict(ownerReferences=[dict(kind='Job', name='same-name', uid='new', controller=True)]))
        self.assertTrue(owned_by(pod, 'Job', 'new'))
        self.assertFalse(owned_by(pod, 'Job', 'old'))
        pod['metadata']['ownerReferences'][0]['controller'] = False
        self.assertFalse(owned_by(pod, 'Job', 'new'))
