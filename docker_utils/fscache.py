"""Read-only observation of JSwarm's existing NFS/CacheFiles tier.

This module never mounts filesystems, starts helpers or changes cache policy.
FS-Cache IO counters are system-wide; NFS READ counters are per visible mount.
"""
from pathlib import Path
import re


def _unescape(value):
    return re.sub(r'\\(040|011|012|134)', lambda m: chr(int(m[1], 8)), value)


def mount_for(path, mountinfo):
    path = Path(path).resolve()
    found = []
    for line in mountinfo.splitlines():
        fields, separator, tail = line.partition(' - ')
        if not separator:
            continue
        left, right = fields.split(), tail.split()
        if len(left) < 6 or len(right) < 3:
            continue
        mount = Path(_unescape(left[4]))
        if path != mount and mount not in path.parents:
            continue
        found.append(dict(mountpoint=str(mount), filesystem=right[0],
                          source=_unescape(right[1]),
                          options=set(left[5].split(',')) | set(right[2].split(','))))
    if not found:
        raise ValueError('No filesystem mount covers the reference path')
    # The last identical mountpoint is the visible overmount.
    return max(enumerate(found), key=lambda v: (len(Path(v[1]['mountpoint']).parts), v[0]))[1]


def mount_stats(text, mountpoint):
    for section in re.split(r'(?m)^device ', text):
        header = section.splitlines()[0] if section.splitlines() else ''
        match = re.search(r' mounted on (.*?) with fstype ', header)
        if not match or _unescape(match[1]) != mountpoint:
            continue
        reads = re.search(r'(?m)^\s*READ:\s*(\d+)', section)
        options = re.search(r'(?m)^\s*opts:\s*(.*)', section)
        return {'network_read_operations': int(reads[1]) if reads else None,
                'options': set(options[1].strip().split(',')) if options else set()}
    return {'network_read_operations': None, 'options': set()}


def _read(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ''


class ReferenceCache:
    def __init__(self, path, proc=Path('/proc')):
        self.proc = Path(proc)
        self.path = Path(path).resolve()
        self.mount = mount_for(self.path, _read(self.proc/'self/mountinfo'))
        stats = mount_stats(_read(self.proc/'self/mountstats'), self.mount['mountpoint'])
        options = self.mount['options'] | stats['options']
        if self.mount['filesystem'] not in ('nfs', 'nfs4') or 'ro' not in self.mount['options']:
            raise ValueError('GPU reference cache requires a read-only NFS mount')
        if not any(o == 'fsc' or o.startswith('fsc=') for o in options):
            raise ValueError('NFS reference mount does not enable FS-Cache')
        self.before = self.snapshot()

    def snapshot(self):
        stats = mount_stats(_read(self.proc/'self/mountstats'), self.mount['mountpoint'])
        match = re.search(r'(?m)^IO\s*:\s*rd=(\d+)', _read(self.proc/'fs/fscache/stats'))
        return {'fscache_read_operations_systemwide': int(match[1]) if match else None,
                'network_read_operations': stats['network_read_operations']}

    def report(self):
        after = self.snapshot()
        delta = {k: after[k]-v if v is not None and after[k] is not None and after[k]>=v else None
                 for k,v in self.before.items()}
        return {'transport': 'nfs-fscache', 'mountpoint': self.mount['mountpoint'],
                'source': self.mount['source'], 'before': self.before, 'after': after,
                'delta': delta, 'systemwide_counters_are_not_job_attribution': True}
