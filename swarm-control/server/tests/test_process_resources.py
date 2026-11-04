import json
import os
from pathlib import Path
import tempfile
import unittest
from process_resources import inspect_process, program_identity, resource_context

class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.proc=Path(self.temp.name)/'proc';self.root=self.proc/'123';(self.root/'fd').mkdir(parents=True);(self.root/'fdinfo').mkdir()
        (self.root/'cmdline').write_bytes(b'python3\0-u\0/work/recovery.py\0--password\0DO_NOT_EXPOSE\0')
        (self.root/'exe').symlink_to('/usr/bin/python3')

    def descriptor(self,number,path,flags):
        (self.root/'fd'/str(number)).symlink_to(path)
        (self.root/'fdinfo'/str(number)).write_text('flags:\t'+oct(flags)[2:]+'\n')

    def test_script_identity_excludes_remaining_arguments_and_inline_code(self):
        identity=program_identity(self.root)
        self.assertEqual(identity['script'],'/work/recovery.py')
        self.assertNotIn('DO_NOT_EXPOSE',json.dumps(identity))
        (self.root/'cmdline').write_bytes(b'python3\0-c\0print("DO_NOT_EXPOSE")\0')
        self.assertNotIn('script',program_identity(self.root))

    def test_descriptor_access_and_database_filenames_are_metadata_not_flow_attribution(self):
        database=Path(self.temp.name)/'object-catalog.sqlite';database.write_bytes(b'not opened by collector')
        download=Path(self.temp.name)/'.download-test';download.write_bytes(b'')
        self.descriptor(3,database,os.O_RDWR);self.descriptor(4,download,os.O_WRONLY);self.descriptor(5,download,os.O_RDONLY)
        result=inspect_process(123,'python3',self.proc)
        self.assertEqual(result['file_count'],2)
        self.assertEqual(result['database_files'],1)
        self.assertEqual(result['writable_files'],2)
        self.assertEqual(result['readable_files'],2)
        self.assertEqual(result['files'][0]['filename'],'object-catalog.sqlite')
        self.assertIn('hint',result['files'][0]['database_hint'])
        self.assertNotIn('not opened by collector',json.dumps(result))

    def test_database_endpoints_keep_engine_inference_separate_from_database_names(self):
        owners=[{'pid':123,'name':'python3'}]
        rows={(6,'10.0.0.1',12345,'10.0.0.2',5432):owners,
              (6,'127.0.0.1',5432,'127.0.0.1',12345):owners,
              (17,'10.0.0.1',12345,'10.0.0.2',5432):owners}
        context=resource_context(rows,self.proc);endpoints=context['processes']['123']['database_endpoints']
        self.assertEqual(len(endpoints),2)
        self.assertEqual(endpoints[0]['engine_hint'],'PostgreSQL')
        self.assertIsNone(endpoints[0]['database_name'])
        self.assertEqual(endpoints[1]['role'],'Local database-port endpoint')

    def test_gone_process_is_unknown_not_empty_verified_file_list(self):
        result=inspect_process(999,'gone',self.proc)
        self.assertEqual(result['status'],'unavailable')
