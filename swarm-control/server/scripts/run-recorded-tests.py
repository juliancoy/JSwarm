#!/usr/bin/env python3
"""Run host unittest suites with a required title and durable per-test events."""
import argparse
import json
from pathlib import Path
import socket
import sys
import time
import unittest
import uuid

server=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(server),str(server/'tests')]
from paths import STATE
from task_events import EventStore

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--title',required=True)
parser.add_argument('tests',nargs='+',help='Unittest modules or test IDs')
args=parser.parse_args()
ident='validation-'+time.strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
store=EventStore();task={'id':ident,'name':args.title,'status':'running','children':[]}
folder=STATE/'tasks';folder.mkdir(exist_ok=True,parents=True)
def save():
    store.observe(task,'registry','host-unittest')
    path=folder/(ident+'.json');temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(task));temp.chmod(0o600);temp.replace(path)
class Result(unittest.TextTestResult):
    def startTest(self,test):
        self.row={'id':test.id(),'name':test.id(),'status':'running','worker':socket.gethostname()}
        task['children'].append(self.row)
        store.append(ident,socket.gethostname(),'test-started',{'test':test.id()});save()
        super().startTest(test)
    def addSuccess(self,test):
        self.row['status']='passed';store.append(ident,socket.gethostname(),'test-passed',{'test':test.id()});super().addSuccess(test)
    def addError(self,test,err):
        self.row['status']='failed';store.append(ident,socket.gethostname(),'test-error',{'test':test.id(),'traceback':self._exc_info_to_string(err,test)});super().addError(test,err)
    def addFailure(self,test,err):
        self.row['status']='failed';store.append(ident,socket.gethostname(),'test-failed',{'test':test.id(),'traceback':self._exc_info_to_string(err,test)});super().addFailure(test,err)
    def addSkip(self,test,reason):
        self.row['status']='skipped';store.append(ident,socket.gethostname(),'test-skipped',{'test':test.id(),'reason':reason});super().addSkip(test,reason)
    def stopTest(self,test):
        save();super().stopTest(test)
save()
result=unittest.TextTestRunner(resultclass=Result,verbosity=1).run(unittest.defaultTestLoader.loadTestsFromNames(args.tests))
task['status']='passed' if result.wasSuccessful() else 'failed';save()
print('Recorded run:',ident)
sys.exit(0 if result.wasSuccessful() else 1)
