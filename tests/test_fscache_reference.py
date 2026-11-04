from pathlib import Path

import pytest
from docker_utils.fscache import ReferenceCache, mount_for


def proc_tree(tmp_path, options='ro,fsc=swarm-reference', filesystem='nfs4'):
    proc = tmp_path/'proc'
    (proc/'self').mkdir(parents=True)
    (proc/'fs/fscache').mkdir(parents=True)
    (proc/'self/mountinfo').write_text(
        '1 0 0:1 / / rw - ext4 /dev/root rw\n'+
        f'2 1 0:2 / /reference {options} - {filesystem} host:/reference ro,fsc\n')
    def stats(reads,hits):
        (proc/'self/mountstats').write_text(
            f'device host:/reference mounted on /reference with fstype nfs4 statvers=1.1\n'
            f' opts: {options}\n READ: {reads} 0 0\n')
        (proc/'fs/fscache/stats').write_text(f'IO     : rd={hits} wr=0 mis=0\n')
    stats(10,20)
    return proc,stats


def test_observe_existing_readonly_fscache_and_report_counter_scope(tmp_path):
    proc,stats = proc_tree(tmp_path)
    cache = ReferenceCache('/reference/gpu-mapper/key',proc)
    stats(10,24)
    report = cache.report()
    assert report['delta'] == dict(fscache_read_operations_systemwide=4, network_read_operations=0)
    assert report['systemwide_counters_are_not_job_attribution'] is True
    (proc/'fs/fscache/stats').unlink()
    assert cache.report()['delta']['fscache_read_operations_systemwide'] is None


def test_reject_noncache_or_writable_mounts_and_choose_longest_path(tmp_path):
    proc,_ = proc_tree(tmp_path, options='rw', filesystem='ext4')
    with pytest.raises(ValueError,match='read-only NFS'):
        ReferenceCache('/reference/key',proc)
    (proc/'self/mountinfo').write_text('2 1 0:2 / /reference ro - nfs4 host:/reference ro\n')
    (proc/'self/mountstats').write_text('')
    with pytest.raises(ValueError,match='does not enable'):
        ReferenceCache('/reference/key',proc)
    info = '1 0 0:1 / / ro - nfs4 host:/ ro,fsc\n2 1 0:2 / /reference\\040cache rw - ext4 /dev/ssd rw\n'
    assert mount_for('/reference cache/key',info)['filesystem'] == 'ext4'
