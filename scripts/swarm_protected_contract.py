"""Public immutable contracts shared by controller, source and protected worker."""
import hashlib
import json
import re


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def job_key(contract):
    validate(contract)
    return hashlib.sha256(canonical(contract)).hexdigest()


def validate(contract):
    required = {'version','run_id','peer','machine','image','argv','inputs','outputs','result_recipient','resources','scratch_mb'}
    if not isinstance(contract,dict) or set(contract)!=required or contract['version']!='genomics.protected.v1':
        raise ValueError('Invalid protected contract')
    def sha(value): return isinstance(value,str) and re.fullmatch('[a-f0-9]{64}',value)
    def name(value): return isinstance(value,str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',value)
    if not isinstance(contract['run_id'],str) or not re.fullmatch('[a-f0-9]{32}',contract['run_id']):
        raise ValueError('Unique immutable run ID required')
    if not name(contract['peer']) or not sha(contract['machine']) or not re.fullmatch('sha256:[a-f0-9]{64}',contract['image']):
        raise ValueError('Pinned worker and image required')
    if (not isinstance(contract['argv'],list) or not 1<=len(contract['argv'])<=128
            or any(not isinstance(v,str) or not v or '\0' in v or len(v)>8192 for v in contract['argv'])):
        raise ValueError('Invalid approved analysis command')
    if not sha(contract['result_recipient']): raise ValueError('Invalid result recipient')
    if not isinstance(contract['inputs'],dict) or not 1<=len(contract['inputs'])<=16:
        raise ValueError('Bounded encrypted inputs required')
    for alias,row in contract['inputs'].items():
        if not name(alias) or not isinstance(row,dict) or set(row)!={'dataset','sha256','bytes','plaintext_sha256','plaintext_bytes'}:
            raise ValueError('Invalid encrypted input')
        if (not name(row['dataset']) or not sha(row['sha256']) or not sha(row['plaintext_sha256'])
                or type(row['bytes']) is not int or row['bytes']<16
                or type(row['plaintext_bytes']) is not int or row['plaintext_bytes']<0):
            raise ValueError('Invalid encrypted input identity')
    if (not isinstance(contract['outputs'],dict) or not 1<=len(contract['outputs'])<=32
            or any(not name(n) or type(v) is not int or v<0 for n,v in contract['outputs'].items())):
        raise ValueError('Explicit output names and byte limits required')
    resources=contract['resources']
    if (not isinstance(resources,dict) or set(resources)!={'cpus','memory_mb'}
            or any(type(v) is not int or v<1 for v in resources.values())
            or type(contract['scratch_mb']) is not int or contract['scratch_mb']<1
            or contract['scratch_mb']+max(1,resources['memory_mb']//8)+1>=resources['memory_mb']):
        raise ValueError('RAM scratch must fit the memory budget')
    # Stage ciphertext and plaintext together, then retain inputs and encrypt outputs.
    minimum=sum(r['bytes']+r['plaintext_bytes'] for r in contract['inputs'].values())
    minimum+=sum(cipher_limit(v)+v for v in contract['outputs'].values())
    if minimum>contract['scratch_mb']*1024**2:
        raise ValueError('Declared payloads and outputs exceed RAM scratch')
    return contract


def cipher_limit(plaintext_bytes):
    return plaintext_bytes+28*((plaintext_bytes+65535)//65536)+1024**2
