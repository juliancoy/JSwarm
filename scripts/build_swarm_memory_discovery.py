"""Build portable discovery wheels without installing packages on fleet hosts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile
from wheel.wheelfile import WheelFile

ZEROCONF='0.152.1'  # Supports the fleet's Python 3.10 and dual-stack interfaces.
IFADDR='0.2.0'


def portable(source,output):
    """Poetry tags SKIP_CYTHON builds as native; verify and correct that tag."""
    with zipfile.ZipFile(source) as archive:
        if any(n.endswith(('.so','.pyd','.dll','.dylib','.exe')) for n in archive.namelist()):
            raise ValueError('Discovery wheel still contains native code')
        metadata=[n for n in archive.namelist() if n.endswith('.dist-info/WHEEL')]
        if len(metadata)!=1:raise ValueError('Unexpected wheel metadata')
        with WheelFile(output,'w') as result:
            for item in archive.infolist():
                if item.filename.endswith('.dist-info/RECORD'):continue
                data=archive.read(item.filename)
                if item.filename==metadata[0]:
                    data=b'Wheel-Version: 1.0\nGenerator: jswarm-verified-pure-python\nRoot-Is-Purelib: true\nTag: py3-none-any\n'
                result.writestr(item,data)
    return {'source_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            'wheel_sha256':hashlib.sha256(Path(output).read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='swarm-discovery-build-') as folder:
        subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--no-binary=zeroconf','--no-cache-dir',
                        '--wheel-dir',folder,'zeroconf=='+ZEROCONF],env={**os.environ,'SKIP_CYTHON':'1'},check=True)
        source=next(Path(folder).glob('zeroconf*.whl'))
        record=portable(source,args.output/('zeroconf-'+ZEROCONF+'-py3-none-any.whl'))
    subprocess.run([sys.executable,'-m','pip','download','--no-deps','--only-binary=:all:','--platform','any',
                    '--dest',str(args.output),'ifaddr=='+IFADDR],check=True)
    record.update(zeroconf=ZEROCONF,ifaddr=IFADDR)
    (args.output/'build.json').write_text(json.dumps(record,indent=2))

if __name__=='__main__':main()
