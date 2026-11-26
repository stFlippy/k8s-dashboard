"""Measure physical HTTP time separately from Quaber's admission wait."""
from contextlib import contextmanager
import threading
import time
from urllib.parse import urlsplit
from quaber.kubernetes import Kubernetes


class MeasuredResponse:
    def __init__(self,response,record,finish):
        self.response,self.record,self.finish=response,record,finish
        self.done=False
    def __getattr__(self,key):return getattr(self.response,key)
    def __enter__(self):return self
    def read(self,*args):
        body=self.response.read(*args)
        self.record['bytes']+=len(body)
        self.record['body_done']=time.perf_counter()
        return body
    def __exit__(self,*args):
        try:return self.response.__exit__(*args)
        finally:
            if not self.done:
                self.done=True
                self.finish(self.record)


class MeasuredOpener:
    def __init__(self,opener,admission_started,finish):
        self.opener,self.admission_started,self.finish=opener,admission_started,finish
    def open(self,request,**kwargs):
        start=time.perf_counter()
        path=urlsplit(request.full_url).path
        positions=[i for p in ('/api/','/apis/') if (i:=path.find(p))>=0]
        if positions:path=path[min(positions):]
        record=dict(path=path,method=request.get_method(),start=start,queue_ms=(start-self.admission_started)*1000,bytes=0)
        try:
            response=self.opener.open(request,**kwargs)
            headers=time.perf_counter()
            record.update(status=response.status,ttfb_ms=(headers-start)*1000,body_done=headers)
            return MeasuredResponse(response,record,self.finish)
        except Exception as exc:
            record.update(status=getattr(exc,'code',0),error=type(exc).__name__,body_done=time.perf_counter())
            self.finish(record)
            raise


class MeasuredKubernetes(Kubernetes):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.http_records=[]
        self.records_lock=threading.Lock()
    def record_http(self,record):
        record['response_ms']=(record.pop('body_done')-record['start'])*1000
        with self.records_lock:self.http_records.append(record)
    @contextmanager
    def _dispatch(self,cid,request,permit=None,*,opener=None):
        measured=MeasuredOpener(self.opener if opener is None else opener,time.perf_counter(),self.record_http)
        with super()._dispatch(cid,request,permit,opener=measured) as response:yield response
    def take_records(self):
        with self.records_lock:
            values,self.http_records=self.http_records,[]
        return values
