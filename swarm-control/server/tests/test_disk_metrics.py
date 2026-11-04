import json
from pathlib import Path
import tempfile
import unittest
from disk_metrics import DiskSampler
from resource_history import project_host


def sample(time,read=0,write=0,identity='disk-one',boot='boot'):
    return {'monotonic':time,'sampled_at':time,'boot_id':boot,'disks':{'sda':{'identity':identity,'read_bytes':read,'write_bytes':write,'busy_ms':time*500,'model':'Fixture disk'}}}


class DiskMetricsTests(unittest.TestCase):
    def test_directions_share_a_sustained_baseline_and_survive_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)/'capacities.json'
            frames=iter([sample(n*5,n*5*60000000,n*5*40000000) for n in range(8)])
            sampler=DiskSampler(lambda _:next(frames),profile)
            for _ in range(6):row=sampler.sample({'host':'worker'})[0]
            self.assertIsNone(row['read_percent'])
            row=sampler.sample({'host':'worker'})[0]
            self.assertEqual(row['capacity_bytes_per_second'],100000000)
            self.assertEqual((row['read_percent'],row['write_percent'],row['total_percent']),(60,40,100))
            saved=json.loads(profile.read_text());self.assertGreaterEqual(saved['worker']['disk-one']['window_seconds'],30)
            sampler=DiskSampler(lambda _:next(frames),profile)
            row=sampler.sample({'host':'worker'})[0]
            self.assertEqual(row['capacity_bytes_per_second'],100000000)
            self.assertIsNone(row['read_percent'])

    def test_burst_does_not_redefine_sustained_capacity_and_can_exceed_100(self):
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)/'capacity.json';profile.write_text(json.dumps({'worker':{'disk-one':{'total_bytes_per_second':100,'basis':'observed sustained','busy_percent':75}}}))
            values=iter([sample(0),sample(5,read=1000,write=500)])
            sampler=DiskSampler(lambda _:next(values),profile);sampler.sample({'host':'worker'});row=sampler.sample({'host':'worker'})[0]
            self.assertEqual((row['read_percent'],row['write_percent'],row['total_percent']),(200,100,300))
            self.assertEqual(row['capacity_bytes_per_second'],100)

    def test_reboot_replaced_disk_and_counter_reset_do_not_create_false_rates(self):
        for after in [sample(5,identity='replacement'),sample(5,boot='new-boot'),sample(5)]:
            with self.subTest(after=after),tempfile.TemporaryDirectory() as folder:
                values=iter([sample(0,read=100),after]);sampler=DiskSampler(lambda _:next(values),Path(folder)/'profile')
                sampler.sample({'host':'worker'});self.assertIsNone(sampler.sample({'host':'worker'})[0]['read_bytes_per_second'])

    def test_projection_keeps_rates_and_baseline_but_excludes_raw_counters(self):
        row=project_host({'disks':[{'name':'sda','read_percent':30,'write_percent':20,'capacity_bytes_per_second':100,'read_bytes':123,'secret':'private'}]})['disks'][0]
        self.assertEqual(row['read_percent'],30);self.assertEqual(row['capacity_bytes_per_second'],100)
        self.assertNotIn('read_bytes',row);self.assertNotIn('secret',row)

    def test_background_trickle_is_not_mistaken_for_disk_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            frames=[sample(n*5,n*1000)for n in range(8)]
            for frame in frames:frame['disks']['sda']['busy_ms']=frame['monotonic']*10
            values=iter(frames);sampler=DiskSampler(lambda _:next(values),Path(folder)/'profile')
            for _ in frames:row=sampler.sample({'host':'worker'})[0]
            self.assertIsNone(row['read_percent']);self.assertNotIn('capacity_bytes_per_second',row)
