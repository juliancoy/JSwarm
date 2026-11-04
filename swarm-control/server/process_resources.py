"""Bounded process identity, open-file metadata, and database endpoint hints."""
import os
from pathlib import Path
import re
import stat

MAX_FILES = 32
MAX_FDS = 2048
MAX_PROCESSES = 64
DATABASE_PORTS = {5432:'PostgreSQL',3306:'MySQL / MariaDB',27017:'MongoDB',6379:'Redis',1433:'SQL Server',9042:'Cassandra',8123:'ClickHouse HTTP'}


def process_start(root):
    try:
        return (Path(root)/'stat').read_text().rsplit(')',1)[1].split()[19]
    except (OSError,IndexError):
        return None


def database_file(path):
    name=Path(path).name.lower().removesuffix(' (deleted)')
    if name.endswith(('.sqlite','.sqlite3')):
        return 'SQLite filename hint'
    if name.endswith('.db'):
        return 'Database filename hint; engine unknown'
    return None


def program_identity(root):
    identity={}
    try:
        identity['executable']=os.readlink(root/'exe').removesuffix(' (deleted)')
    except OSError:
        pass
    # Select only the interpreter's script argument. Never return argument lists,
    # connection strings, environment variables, query strings, or credentials.
    try:
        with (root/'cmdline').open('rb') as stream:args=stream.read(65536).split(b'\0')
        program=Path(args[0].decode(errors='replace')).name
        interpreter=re.fullmatch(r'python(?:\d+(?:\.\d+)*)?|node|nodejs|ruby|perl|php',program)
        if not interpreter:return identity
        for raw in args[1:]:
            arg=raw.decode(errors='replace')
            if arg in ('-u','-B','-O','-OO','-I','-E','-s','-S','-q'):continue
            if not arg or arg.startswith('-') or '\n' in arg or '://' in arg:break
            suffix=Path(arg).suffix.lower()
            if suffix in ('.py','.js','.mjs','.cjs','.rb','.pl','.php'):
                path=Path(arg)
                if not path.is_absolute():path=Path(os.readlink(root/'cwd'))/path
                identity['script']=str(path)
            break
    except (OSError,ValueError):
        pass
    return identity


def inspect_process(pid, name, proc=Path('/proc')):
    root=Path(proc)/str(pid)
    result={'pid':pid,'name':name,'start_ticks':process_start(root),**program_identity(root),'files':[],'file_count':0,'readable_files':0,'writable_files':0,'database_files':0,'status':'unavailable'}
    try:
        descriptors=sorted((root/'fd').iterdir(),key=lambda p:int(p.name))
    except OSError:
        return result
    files={}
    for fd in descriptors[:MAX_FDS]:
        try:
            path=os.readlink(fd)
            if not path.startswith('/') or not stat.S_ISREG(fd.stat().st_mode):continue
            flags=next(line.split()[1] for line in (root/'fdinfo'/fd.name).read_text().splitlines() if line.startswith('flags:'))
            flags=int(flags,8)
            if flags & getattr(os,'O_PATH',0):continue
            access=flags & os.O_ACCMODE
            read=access in (os.O_RDONLY,os.O_RDWR);write=access in (os.O_WRONLY,os.O_RDWR)
            entry=files.setdefault(path,{'path':path,'filename':Path(path).name,'readable':False,'writable':False,'database_hint':database_file(path)})
            entry['readable'] |= read;entry['writable'] |= write
        except (OSError,ValueError,StopIteration):
            continue
    if process_start(root)!=result['start_ticks']:
        return result
    values=sorted(files.values(),key=lambda f:(not bool(f['database_hint']),not f['writable'],f['path']))
    result.update(status='available',files=values[:MAX_FILES],file_count=len(values),
                  readable_files=sum(f['readable'] for f in values),writable_files=sum(f['writable'] for f in values),
                  database_files=sum(bool(f['database_hint']) for f in values),fd_scan_limited=len(descriptors)>MAX_FDS,
                  files_truncated=len(values)>MAX_FILES)
    return result


def resource_context(rows, proc=Path('/proc')):
    names={}
    for owners in rows.values():
        for owner in owners:
            if len(names)<MAX_PROCESSES or owner['pid'] in names:names[owner['pid']]=owner['name']
    processes={str(pid):inspect_process(pid,name,proc) for pid,name in names.items()}
    for process in processes.values():process['database_endpoints']=[]
    for (protocol,local,lp,remote,rp),owners in rows.items():
        if protocol!=6 or rp not in DATABASE_PORTS and lp not in DATABASE_PORTS:continue
        remote_service=rp in DATABASE_PORTS
        port=rp if remote_service else lp
        endpoint={'address':remote if remote_service else local,'port':port,'engine_hint':DATABASE_PORTS[port],
                  'role':'Remote database-port endpoint' if remote_service else 'Local database-port endpoint',
                  'evidence':'Connected TCP socket; engine inferred from service port',
                  'database_name':None}
        for owner in owners:
            process=processes.get(str(owner['pid']))
            if process is not None and endpoint not in process['database_endpoints']:
                if len(process['database_endpoints'])<16:process['database_endpoints'].append(endpoint)
    return {'processes':processes,'max_processes':MAX_PROCESSES,'max_files_per_process':MAX_FILES,
            'scope':'Current host process context, not per-flow file attribution. Read/write means the descriptor permits that operation, not that bytes moved. File and database hints do not identify network payloads. TCP endpoint ports suggest a database engine, not a database name. File contents, SQL, remaining command arguments and environments are not collected.'}
