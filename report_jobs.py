"""Bounded process-local report queue. Single web process; restart loses jobs."""
import time, threading, secrets
from concurrent.futures import ThreadPoolExecutor
class ReportJobs:
    def __init__(self):
        self.lock=threading.RLock(); self.jobs={}; self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='report')
    def submit(self,owner,scope,extension,work):
        with self.lock:
            now=time.monotonic()
            for k,j in list(self.jobs.items()):
                if j['status'] in ('ready','failed') and now-j['updated']>900: del self.jobs[k]
            if len(self.jobs)>=12 or sum(j['owner']==owner and j['status'] in ('queued','running') for j in self.jobs.values())>=2: raise ValueError('Report queue is busy; try again later.')
            key=secrets.token_urlsafe(24); self.jobs[key]=dict(owner=owner,scope=scope,extension=extension,status='queued',updated=now)
        def run():
            with self.lock: self.jobs[key]['status']='running'
            try:
                data,name,mime=work()
                if len(data)>40*1024*1024: raise ValueError('Report exceeds size limit')
                update=dict(status='ready',data=data,name=name,mime=mime)
            except Exception:
                import logging; logging.getLogger(__name__).exception('Report job failed');update=dict(status='failed')
            with self.lock: self.jobs[key].update(update,updated=time.monotonic())
        self.pool.submit(run);return key
    def get(self,key,owner,scope,extension=None):
        with self.lock:
            j=self.jobs.get(key)
            if not j or j['owner']!=owner or j['scope']!=scope or (extension and j['extension']!=extension): return None
            if j['status'] in ('ready','failed') and time.monotonic()-j['updated']>900: del self.jobs[key];return None
            return dict(j)
    def remove(self,key):
        with self.lock:self.jobs.pop(key,None)
