"""Deterministic source bundle for the existing signed swarm-node release."""
import base64
import io
from pathlib import Path
import zipfile


def encoded_runtime(root):
    root=Path(root)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.glob('*.py')):
            if path.is_symlink():raise ValueError('Runtime sources cannot be symlinks')
            data=path.read_bytes();compile(data,str(path),'exec')
            info=zipfile.ZipInfo('ramtop/'+path.name,date_time=(1980,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,data)
    if len(output.getvalue())>256000:raise ValueError('Monitor runtime bundle exceeds its budget')
    return base64.b64encode(output.getvalue()).decode()
