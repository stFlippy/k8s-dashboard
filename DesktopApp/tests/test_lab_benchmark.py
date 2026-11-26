"""Protect lab cleanup boundaries and the meaning of API timing numbers."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.request import Request
from benchmarks.http_timing import MeasuredOpener
from benchmarks.lab_dataset import LabDataset


class LabBenchmarkTests(unittest.TestCase):
    def test_response_time_excludes_admission_and_post_read_parsing(self):
        now=[10.]
        values=[]
        class Response:
            status=200
            def read(self,*_):
                now[0]+=.1
                return b''
            def __exit__(self,*_):return False
        class Opener:
            def open(self,*_,**__):
                now[0]+=.2
                return Response()
        with patch('benchmarks.http_timing.time.perf_counter',side_effect=lambda:now[0]):
            opener=MeasuredOpener(Opener(),9.,values.append)
            with opener.open(Request('https://lab.invalid/secret-prefix/api/v1/pods')) as response:
                response.read()
                now[0]+=5  # Work after HTTP EOF, such as decoding JSON.
        self.assertEqual(len(values),1)
        record=values[0]
        self.assertEqual(record['path'],'/api/v1/pods')
        self.assertAlmostEqual(record['queue_ms'],1000)
        self.assertAlmostEqual(record['ttfb_ms'],200)
        self.assertAlmostEqual((record['body_done']-record['start'])*1000,300)

    def test_cleanup_rejects_foreign_namespace_and_uses_owned_uid(self):
        dataset=LabDataset.__new__(LabDataset)
        dataset.run='test'
        dataset.owned=[dict(name='production',uid='p')]
        dataset.request=Mock()
        with self.assertRaises(ValueError):dataset.cleanup()
        dataset.request.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            dataset.state=Path(directory)/'cleanup.json'
            dataset.owned=[dict(name='quaber-load-test-01',uid='owned-uid')]
            dataset.request=Mock(side_effect=[dict(items=[]),{},dict(items=[])])
            dataset.cleanup()
            deletion=dataset.request.call_args_list[1]
            self.assertEqual(deletion.args[0],'DELETE')
            self.assertEqual(deletion.args[2]['preconditions'],dict(uid='owned-uid'))
            self.assertIn('verified absent',dataset.state.read_text())

    def test_cleanup_recovers_only_labelled_namespace_after_lost_create_response(self):
        dataset=LabDataset.__new__(LabDataset)
        dataset.run='test'
        dataset.owned=[]
        with tempfile.TemporaryDirectory() as directory:
            dataset.state=Path(directory)/'cleanup.json'
            resource=dict(metadata=dict(name='quaber-load-test-01',uid='lost-response-uid',labels={'quaber-benchmark':'test'}))
            dataset.request=Mock(side_effect=[dict(items=[resource]),{},dict(items=[])])
            dataset.cleanup()
            self.assertEqual(dataset.request.call_args_list[1].args[2]['preconditions'],dict(uid='lost-response-uid'))


if __name__=='__main__':unittest.main()
