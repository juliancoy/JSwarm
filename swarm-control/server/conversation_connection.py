"""Bounded attachment of existing IDE conversations; never starts a model turn."""
import threading
import time


class ConnectionRecovery:
    def __init__(self,opener,clock=time.monotonic,start=None):
        self.opener=opener;self.clock=clock;self.lock=threading.Lock()
        self.entries={};self.opening=None
        self.start=start or (lambda work:threading.Thread(target=work,name='conversation-connect',daemon=True).start())

    def status(self,ident):
        with self.lock:
            entry=self.entries.get(ident)
            if not entry:return 'detached'
            return 'needs_attention' if entry['attempts']>=3 and self.opening!=ident and self.clock()>=entry['next'] else 'connecting'

    def connected(self,ident):
        with self.lock:self.entries.pop(ident,None)

    def request(self,ident,retry=False):
        with self.lock:
            # Bound state to conversations actually requested, not polling history.
            if len(self.entries)>=100 and ident not in self.entries:return 'needs_attention'
            entry=self.entries.setdefault(ident,{'attempts':0,'next':0})
            if retry and self.opening!=ident:entry.update(attempts=0,next=0)
            if self.opening is not None:return self.status_unlocked(ident)
            if entry['attempts']>=3:return self.status_unlocked(ident)
            if self.clock()<entry['next']:return 'connecting'
            entry['attempts']+=1;entry['next']=self.clock()+min(120,15*2**(entry['attempts']-1));self.opening=ident
        def work():
            try:self.opener(ident)
            except Exception:pass  # No private thread or message contents in logs.
            finally:
                with self.lock:self.opening=None
        try:self.start(work)
        except Exception:
            with self.lock:self.opening=None
            raise
        return 'connecting'

    def status_unlocked(self,ident):
        entry=self.entries[ident]
        return 'needs_attention' if entry['attempts']>=3 and self.opening!=ident and self.clock()>=entry['next'] else 'connecting'
