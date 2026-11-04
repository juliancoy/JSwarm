"""Shared, bounded operations for the test master's REST and MCP interfaces."""
import argparse
import copy
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs,unquote,urlsplit

TOKEN_FILE=Path.home()/'.config/map-master/api-token'
API='/api/v1/master'
RUN_ID={'type':'string','pattern':r'^[0-9TZ-]+-[a-f0-9]{8}$','description':'Recorded run ID; omit to select the latest run.'}
TOOLS=[
 ('request_research','Queue private DNA analysis','Queue an owner-configured private research profile. No file paths or genomic payloads accepted.',{'profile':{'type':'string','minLength':1,'maxLength':40},'request_id':{'type':'string','minLength':36,'maxLength':36}},['profile','request_id']),
 ('cancel_request','Cancel a queued or running job','Cancel one durable job request without stopping other runs.',{'request_id':{'type':'string','minLength':36,'maxLength':36}},['request_id']),
 ('list_research_profiles','Configured private analyses','List profile names only; never private input paths.',{},[]),
 ('get_runtime_stats','Test runtime statistics','Read persistent per-task and per-machine runtime samples, median and p95. Reused results are excluded from timing percentiles.',{'host':{'type':'string','minLength':1,'maxLength':253},'task':{'type':'string','minLength':1,'maxLength':100}},[]),
 ('create_backups','Back up enrolled devices','Replicate current production data from the latest complete DR snapshot as encrypted, verified device copies. Historical public dataset versions and private decryption keys stay on the recovery host.',{},[]),
 ('get_backups','Backup status','Read persistent per-device backup progress, verification receipts and failures.',{},[]),
 ('request_tests','Request focused tests','Queue tests for repository-relative code paths or named suites. COMMITS AND PUSHES current sources before execution. Uses available workers; no deployment or email checks.',{'paths':{'type':'array','items':{'type':'string'},'maxItems':100},'suites':{'type':'array','items':{'type':'string'},'maxItems':100},'force':{'type':'boolean','default':False}},[]),
 ('list_test_requests','Test request inbox','List the latest 100 test requests and their run IDs.',{},[]),
 ('get_test_request','Test request status','Read request status, runtime and associated run ID.',{'request_id':{'type':'string','minLength':36,'maxLength':36}},['request_id']),
 ('get_status','Master status','Inspect the current distributed run and preparation without starting work.',{},[]),
 ('list_runs','Run history','List the latest ten independent test run summaries.',{},[]),
 ('get_run','Run details','Read one run, its source revisions and per-machine suite results. Logs are accessed separately.',{'run_id':RUN_ID},[]),
 ('get_logs','Suite logs','Read bounded recorded output for one machine/suite in a run.',{'run_id':RUN_ID,'host':{'type':'string','minLength':1,'maxLength':253},'suite':{'type':'string','minLength':1,'maxLength':80},'tail':{'type':'integer','minimum':1,'maximum':80,'default':40}},['host','suite']),
 ('list_machines','Machine inventory','Inspect observed LAN machines and machines recorded in the latest run. Does not probe or initialize agents.',{},[]),
 ('list_suites','Configured suites','List application-owned suites and their container images; arbitrary commands cannot be submitted.',{},[]),
 ('start_run','Start distributed tests','Start the configured distributed run. The coordinator COMMITS AND PUSHES the current Organization, OrgPortal, PIdP and CodeCollective branches to GitHub, prepares agents/images, and runs suites. Does not deploy production services.',{},[]),
 ('rerun_suite','Rerun one suite','Queue an independent fresh execution on available workers using current published sources. Commits and pushes current branches. No deployment or email checks.',{'run_id':RUN_ID,'host':{'type':'string','minLength':1,'maxLength':253},'suite':{'type':'string','minLength':1,'maxLength':80}},['run_id','host','suite']),
 ('rerun_failed','Rerun failed suites','Queue failed ordinary suites from a recorded run. P2P requires a full run. Commits and pushes current branches; no deployment or email checks.',{'run_id':RUN_ID},['run_id']),
 ('stop_run','Stop a specific run','Request cancellation of the currently running test run by ID. Refuses to stop a newer, different run.',{'run_id':RUN_ID},['run_id']),
]


class APIError(Exception):
    def __init__(self,status,code,message):
        self.status,self.code,self.message=status,code,message
        super().__init__(message)


def tool_definitions():
    return [{'name':name,'title':title,'description':description,
             'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False},
             'annotations':{'readOnlyHint':name not in ('start_run','stop_run','rerun_suite','rerun_failed','request_tests','create_backups','request_research','cancel_request'),
                            'destructiveHint':name in ('start_run','stop_run','rerun_suite','rerun_failed','request_tests','create_backups','request_research','cancel_request'),
                            'idempotentHint':name not in ('start_run','rerun_suite','rerun_failed','request_tests','create_backups'),'openWorldHint':name in ('start_run','rerun_suite','rerun_failed','request_tests','create_backups')}}
            for name,title,description,properties,required in TOOLS]


def validate(name,arguments):
    import re
    definition=next((tool for tool in tool_definitions() if tool['name']==name),None)
    if not definition:raise APIError(404,'unknown_operation','Unknown master operation')
    schema=definition['inputSchema']
    if not isinstance(arguments,dict) or set(arguments)-set(schema['properties']) or set(schema['required'])-set(arguments):
        raise APIError(400,'invalid_arguments','Invalid or missing operation arguments')
    for key,value in arguments.items():
        rule=schema['properties'][key]
        if rule['type']=='string':
            if not isinstance(value,str) or not rule.get('minLength',0)<=len(value)<=rule.get('maxLength',200):
                raise APIError(400,'invalid_arguments','Invalid '+key)
            if rule.get('pattern') and not re.fullmatch(rule['pattern'],value):raise APIError(400,'invalid_arguments','Invalid '+key)
        elif rule['type']=='boolean':
            if type(value) is not bool:raise APIError(400,'invalid_arguments','Invalid '+key)
        elif rule['type']=='array':
            if not isinstance(value,list) or len(value)>rule['maxItems'] or any(not isinstance(v,str) or not 1<=len(v)<=300 for v in value):raise APIError(400,'invalid_arguments','Invalid '+key)
        elif rule['type']=='integer':
            if type(value) is not int or not rule['minimum']<=value<=rule['maximum']:raise APIError(400,'invalid_arguments','Invalid '+key)
    return arguments


class MasterAPI:
    def __init__(self,tests,devices=lambda:{},suites=lambda:{}):
        self.tests,self.devices,self.suites=tests,devices,suites
        self._backups=None
        self.lock=getattr(tests,'lock',threading.RLock())

    def call(self,name,arguments=None):
        args=validate(name,{} if arguments is None else arguments)
        try:
            if name in ('create_backups','get_backups'):
                with self.lock:
                    if self._backups is None:
                        from fleet_backups import FleetBackups
                        self._backups=FleetBackups(self.tests)
                return self._backups.start() if name=='create_backups' else self._backups.view()
            if name=='request_research':return self.tests.requests.submit_research(args['profile'],args['request_id'])
            if name=='cancel_request':return {'accepted':self.tests.requests.cancel(args['request_id'])}
            if name=='list_research_profiles':
                from research_jobs import profiles
                return {'profiles':sorted(profiles())}
            if name=='get_runtime_stats':
                from runtime_stats import report
                return report(self.tests.runner.folder,**args)
            if name=='request_tests':return self.tests.requests.submit(**args)
            if name=='list_test_requests':return self.tests.requests.view()
            if name=='get_test_request':return self.tests.requests.view(args['request_id'])
            if name=='list_suites':
                return {'suites':[{'id':key,'name':value['name'],'image':value.get('image'),'coordinated':bool(value.get('coordinated'))}
                                  for key,value in self.suites().items()]}
            if name=='list_machines':
                view=self.tests.view();run=view.get('run') or {}
                fields=('ip','ipv6','hostname','self','status')
                from mobile_worker import discover
                return {'mobile_workers':discover()['devices'],'observed':[ {key:device.get(key) for key in fields if key in device} for device in self.devices().get('devices',[])],
                        'run_id':run.get('id'),'workers':[self.machine(host) for host in run.get('hosts',[])]}
            if name in ('rerun_suite','rerun_failed'):
                ident=self.tests.rerun(args['run_id'],args.get('host'),args.get('suite'),failed_only=name=='rerun_failed')
                return {'accepted':True,'run_id':ident,'parent_run_id':args['run_id']}
            if name in ('start_run','stop_run'):
                with self.lock:
                    if name=='start_run':
                        accepted=self.tests.start()
                    else:
                        current=self.tests.view(args['run_id']).get('run') or {}
                        if current.get('id')!=args['run_id']:raise APIError(409,'run_mismatch','This ID is not the current run; no run was stopped')
                        accepted=self.tests.stop_run(args['run_id'])
                    if not accepted:raise APIError(409,'run_conflict','A run is already active' if name=='start_run' else 'Run is not active or is already stopping')
                    return {'accepted':True,**self.status()}
            if name=='get_status':return self.status()
            if name=='list_runs':
                view=self.tests.view()
                return {'running':view['running'],'runs':view.get('history',[])[:10],'limit':10}
            view=self.tests.view(args.get('run_id'))
            run=view.get('run')
            if not run:raise APIError(404,'run_not_found','No recorded run exists')
            if name=='get_run':
                result=copy.deepcopy(run)
                for host in result.get('hosts',[]):
                    for job in host.get('jobs',{}).values():
                        job['log_lines']=len(job.pop('logs',[]))
                        job['failure_lines']=len(job.pop('failure_details',[]))
                return {'running':view['running'] and not run.get('finished'),'run':result}
            if name=='get_logs':
                host=next((host for host in run.get('hosts',[]) if host['host']==args['host']),None)
                job=(host or {}).get('jobs',{}).get(args['suite'])
                if job is None:raise APIError(404,'suite_not_found','Machine/suite was not recorded in this run')
                return {'run_id':run['id'],'host':args['host'],'suite':args['suite'],'status':job.get('status'),
                        'lines':[str(line)[:2000] for line in job.get('logs',[])[-args.get('tail',40):]],
                        'failures':[str(line)[:2000] for line in job.get('failure_details',[])[-20:]],'retained_lines':len(job.get('logs',[]))}
        except APIError:raise
        except ValueError as error:raise APIError(404 if args.get('run_id') else 400,'invalid_request',str(error)) from None
        except (OSError,RuntimeError):raise APIError(503,'master_unavailable','Master operation unavailable; inspect the local service logs') from None

    @staticmethod
    def machine(host):
        return {key:copy.deepcopy(host[key]) for key in ('host','local','status','cpus','capacity','active_tasks','session','error','excluded','hardware') if key in host}

    def status(self):
        view=self.tests.view();run=view.get('run')
        return {'service':'map-test-master','running':view['running'],'active_runs':view.get('active_runs',[]),'snapshot_staging':view.get('snapshot_staging'),'run':None if not run else
                {key:copy.deepcopy(run.get(key)) for key in ('id','status','started','finished','preparation','image_progress','error','granular','pending_tasks')},
                'links':{'dashboard':'/tests.html','mcp':'/mcp','api':API,'openapi':API+'/openapi.json'}}

    def rest(self,method,target,body=None):
        url=urlsplit(target);path=url.path.rstrip('/');query=parse_qs(url.query,keep_blank_values=True)
        def parameters(allowed):
            if set(query)-set(allowed) or any(len(values)!=1 for values in query.values()):raise APIError(400,'invalid_query','Invalid query parameters')
            return {key:values[0] for key,values in query.items()}
        if path.startswith(API+'/codex/'):
            import codex
            return codex.rest(method,path,query,body)
        if method=='GET':
            if path==API+'/research-profiles':return 200,self.call('list_research_profiles')
            if path==API+'/backups':return 200,self.call('get_backups')
            if path==API+'/runtime-stats':return 200,self.call('get_runtime_stats',parameters(('host','task')))
            if path==API+'/test-requests':return 200,self.call('list_test_requests')
            if path.startswith(API+'/test-requests/'):
                return 200,self.call('get_test_request',{'request_id':path.rsplit('/',1)[1]})
            if path==API+'/openapi.json':return 200,self.openapi()
            mapping={API:'get_status',API+'/status':'get_status',API+'/runs':'list_runs',API+'/machines':'list_machines',API+'/suites':'list_suites'}
            if path in mapping:
                parameters(())
                return 200,self.call(mapping[path])
            if path.startswith(API+'/runs/'):
                pieces=path[len(API+'/runs/'):].split('/')
                ident=unquote(pieces[0])
                if len(pieces)==1:
                    parameters(())
                    return 200,self.call('get_run',{'run_id':ident})
                if len(pieces)==2 and pieces[1]=='logs':
                    args=parameters(('host','suite','tail'));args['run_id']=ident
                    if 'tail' in args:
                        try:args['tail']=int(args['tail'])
                        except ValueError:raise APIError(400,'invalid_query','tail must be an integer') from None
                    return 200,self.call('get_logs',args)
        elif method=='POST':
            parameters(())
            if path==API+'/research-requests':return 202,self.call('request_research',body)
            if path==API+'/cancel-request':return 202,self.call('cancel_request',body)
            if path==API+'/backups':
                if body not in ({},None):raise APIError(400,'invalid_body','Backups use the latest complete DR snapshot')
                return 202,self.call('create_backups')
            if path==API+'/test-requests':return 202,self.call('request_tests',body)
            if path==API+'/rerun-suite':return 202,self.call('rerun_suite',body)
            if path==API+'/rerun-failed':return 202,self.call('rerun_failed',body)
            if body not in ({},None):raise APIError(400,'invalid_body','This endpoint accepts an empty JSON object')
            if path==API+'/runs':return 202,self.call('start_run')
            if path.startswith(API+'/runs/') and path.endswith('/stop'):
                ident=unquote(path[len(API+'/runs/'):-len('/stop')])
                return 202,self.call('stop_run',{'run_id':ident})
        raise APIError(404,'not_found','Unknown master API route')

    @staticmethod
    def openapi():
        paths={}
        routes=[('post','/research-requests','request_research'),('post','/cancel-request','cancel_request'),('get','/research-profiles','list_research_profiles'),('post','/backups','create_backups'),('get','/backups','get_backups'),('post','/test-requests','request_tests'),('get','/test-requests','list_test_requests'),('get','/test-requests/{request_id}','get_test_request'),('post','/rerun-suite','rerun_suite'),('post','/rerun-failed','rerun_failed'),('get','/status','get_status'),('get','/runs','list_runs'),('post','/runs','start_run'),
                ('get','/runs/{run_id}','get_run'),('get','/runs/{run_id}/logs','get_logs'),
                ('post','/runs/{run_id}/stop','stop_run'),('get','/machines','list_machines'),('get','/suites','list_suites')]
        definitions={tool['name']:tool for tool in tool_definitions()}
        for method,path,name in routes:
            tool=definitions[name];operation={'operationId':name,'summary':tool['title'],'description':tool['description'],'responses':{
                '202' if method=='post' else '200':{'description':'Accepted' if method=='post' else 'Success','content':{'application/json':{'schema':{'type':'object'}}}},
                '400':{'description':'Invalid arguments'},'401':{'description':'Bearer token required'},'409':{'description':'Run conflict'},'503':{'description':'Master unavailable'}}}
            parameters=[]
            for key,schema in tool['inputSchema']['properties'].items():
                if key in ('run_id','request_id'):parameters.append({'name':key,'in':'path','required':True,'schema':schema})
                else:parameters.append({'name':key,'in':'query','required':key in tool['inputSchema']['required'],'schema':schema})
            if name in ('rerun_suite','rerun_failed','request_tests','create_backups'):
                operation['requestBody']={'required':True,'content':{'application/json':{'schema':tool['inputSchema']}}}
            elif parameters:operation['parameters']=parameters
            paths.setdefault(API+path,{})[method]=operation
        return {'openapi':'3.1.0','info':{'title':'Map Distributed Test Master','version':'1.0.0'},'servers':[{'url':'/'}],
                'security':[{'bearerAuth':[]}],'components':{'securitySchemes':{'bearerAuth':{'type':'http','scheme':'bearer'}}},'paths':paths}


def authorized(headers,scope='master'):
    defaults={'master':TOKEN_FILE,'codex':TOKEN_FILE.with_name('codex-token'),'launcher':TOKEN_FILE.with_name('launcher-token'),'pairing':TOKEN_FILE.with_name('pairing-token')}
    envs={'master':'MAP_MASTER_TOKEN_FILE','codex':'MAP_CODEX_TOKEN_FILE','launcher':'MAP_LAUNCHER_TOKEN_FILE','pairing':'MAP_PAIRING_TOKEN_FILE'}
    path=Path(os.getenv(envs[scope],str(defaults[scope])))
    try:expected=path.read_text().strip()
    except OSError:return False
    provided=headers.get('Authorization','')
    return len(expected)>=32 and hmac.compare_digest(provided.encode(),('Bearer '+expected).encode())


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['create-token']);parser.add_argument('--scope',choices=['master','codex','launcher','pairing'],default='master');args=parser.parse_args()
    scope=args.scope
    path=Path(os.getenv({'master':'MAP_MASTER_TOKEN_FILE','codex':'MAP_CODEX_TOKEN_FILE','launcher':'MAP_LAUNCHER_TOKEN_FILE','pairing':'MAP_PAIRING_TOKEN_FILE'}[scope],str(TOKEN_FILE if scope=='master' else TOKEN_FILE.with_name(scope+'-token'))))
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:stream.write(secrets.token_urlsafe(48)+'\n')
    print('Master API token created at '+str(path)+' (token not printed)')


if __name__=='__main__':main()
