"""Fail-closed checks of committed release blobs; diagnostics never include values."""
import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    fingerprint: str = ""


RULES = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b'),
    'OpenAI token': re.compile(rb'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b'),
    'Stripe secret key': re.compile(rb'\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b'),
    'AWS access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'Slack token': re.compile(rb'\bxox[baprs]-[A-Za-z0-9-]{20,}\b'),
    'Google API key': re.compile(rb'\bAIza[0-9A-Za-z_-]{35}\b'),
    'service account key': re.compile(rb'"type"\s*:\s*"service_account"'),
    'credential in URL': re.compile(rb'\b[a-z][a-z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@',re.I),
    'secret assignment': re.compile(rb'''["']?\b(?:[a-z0-9_]*(?:password|passwd|secret|private_key|api_key|access_token|auth_token|signing_key))["']?\s*[:=]\s*["']([^"'\r\n]{16,})["']''',re.I),
}
PLACEHOLDER = re.compile(rb'^(?:\$|<|\{|your[-_ ]|example|dummy|fake|test[-_ ]|placeholder|changeme|replace[-_ ]|https?://)',re.I)


def sensitive_path(path):
    name=Path(path).name.lower()
    if name.endswith(('.example','.sample','.template','.dist')):return False
    return (name=='.env' or name.startswith('.env.') or
            name in ('id_rsa','id_dsa','id_ecdsa','id_ed25519','credentials.json','service-account.json') or
            name.endswith(('.p12','.pfx','.key','credential.json')))


def scan_chunk(path,data,offset=0):
    findings=[]
    for rule,pattern in RULES.items():
        for match in pattern.finditer(data):
            if rule in ('secret assignment','credential in URL'):
                value=match.group(1)
                if PLACEHOLDER.search(value):continue
            findings.append(Finding(path,offset+data[:match.start()].count(b'\n')+1,rule,hashlib.sha256(match.group(0)).hexdigest()))
    return findings


def scan_git(repository,revision='HEAD'):
    """Read pinned Git objects, including tracked ignored/export-ignored files."""
    repository=Path(repository)
    tree=subprocess.check_output(['git','-C',str(repository),'ls-tree','-rz','--full-tree',revision])
    findings=set()
    with subprocess.Popen(['git','-C',str(repository),'cat-file','--batch'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL) as proc:
        try:
            for entry in tree.split(b'\0'):
                if not entry:continue
                metadata,path=entry.split(b'\t',1);mode,kind,oid=metadata.split()
                if kind!=b'blob':continue
                path=path.decode('utf-8','replace')
                if sensitive_path(path):findings.add(Finding(path,1,'sensitive filename'))
                proc.stdin.write(oid+b'\n');proc.stdin.flush()
                header=proc.stdout.readline().split()
                if len(header)!=3 or header[1]!=b'blob':raise RuntimeError('Cannot read committed source for secret scan')
                remaining=int(header[2]);tail=b'';line=0
                while remaining:
                    chunk=proc.stdout.read(min(1024*1024,remaining))
                    if not chunk:raise RuntimeError('Incomplete committed source during secret scan')
                    remaining-=len(chunk);data=tail+chunk
                    findings.update(scan_chunk(path,data,line))
                    keep=data[-4096:];line+=data[:-len(keep)].count(b'\n') if keep else data.count(b'\n');tail=keep
                if proc.stdout.read(1)!=b'\n':raise RuntimeError('Invalid Git blob framing')
        finally:
            proc.stdin.close()
        if proc.wait()!=0:raise RuntimeError('Git secret scan failed')
    return sorted(findings,key=lambda f:(f.path,f.line,f.rule))


def allowed(repository,finding):
    path=Path(__file__).with_name('release-secrets-allowlist.json')
    entries=json.loads(path.read_text()) if path.exists() else []
    return any(entry.get('repository')==repository and entry.get('path')==finding.path and
               entry.get('rule')==finding.rule and entry.get('sha256')==finding.fingerprint and
               bool(entry.get('reason')) and finding.fingerprint for entry in entries)


def check_repositories(repositories):
    failures=[]
    for name,folder,revision in repositories:
        print(f'[secrets] Checking committed files in {name} ({revision[:12]})…',flush=True)
        findings=scan_git(folder,revision)
        failures.extend(f'{name}/{f.path}:{f.line}: {f.rule}' for f in findings if not allowed(name,f))
    if failures:
        raise RuntimeError('Sensitive-data check blocked publication (values redacted):\n'+'\n'.join(failures))
    print('[secrets] Committed-source checks passed.',flush=True)


def check_tree(root):
    """Recheck the exported bytes before executing frozen-source validation."""
    findings=[]
    for path in sorted(Path(root).rglob('*')):
        if path.is_symlink() or not path.is_file():continue
        relative=str(path.relative_to(root))
        if sensitive_path(relative):findings.append(Finding(relative,1,'sensitive filename'))
        with path.open('rb') as stream:
            tail=b'';line=0
            while chunk:=stream.read(1024*1024):
                data=tail+chunk;findings.extend(scan_chunk(relative,data,line))
                keep=data[-4096:];line+=data[:-len(keep)].count(b'\n') if keep else data.count(b'\n');tail=keep
    if findings:
        rows=[]
        for finding in sorted(set(findings),key=lambda f:(f.path,f.line,f.rule)):
            parts=Path(finding.path).parts
            repository=parts[0] if parts[0] in ('JSwarm','GenomeHarbor') else 'Organization'
            relative=str(Path(*parts[1:])) if repository!='Organization' else finding.path
            if not allowed(repository,Finding(relative,finding.line,finding.rule,finding.fingerprint)):rows.append(finding)
        if not rows:return
        raise RuntimeError('Sensitive-data check blocked publication (values redacted):\n'+'\n'.join(f'{f.path}:{f.line}: {f.rule}' for f in rows))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository',nargs='?',default='.')
    parser.add_argument('--revision',default='HEAD')
    args=parser.parse_args()
    try:check_repositories([(Path(args.repository).name,args.repository,args.revision)])
    except (RuntimeError,subprocess.SubprocessError) as error:
        if isinstance(error,subprocess.SubprocessError):parser.exit(1,'Unable to read Git sources; secret scan failed.\n')
        parser.exit(1,str(error)+'\n')


if __name__=='__main__':main()
