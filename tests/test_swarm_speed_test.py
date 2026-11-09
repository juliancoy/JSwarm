import io
import sys
import pytest
from scripts.swarm_speed_test import transfer, measure


def test_stream_verification_and_truncation():
    output = io.BytesIO()
    digest = transfer(None, output, 'send', 300001)
    assert transfer(io.BytesIO(output.getvalue()), None, 'receive', 300001) == digest
    with pytest.raises(RuntimeError, match='ended'):
        transfer(io.BytesIO(b'short'), None, 'receive', 100)


@pytest.mark.parametrize('direction,mode', [('upload', 'receive'), ('download', 'send')])
def test_transfer_subprocess(direction, mode):
    command = [sys.executable, '-c', f'from scripts.swarm_speed_test import receiver_main; print("READY", flush=True); receiver_main({mode!r}, 300001)']
    result = measure(command, direction, 300001, 5, handshake=True)
    assert result['bytes'] == 300001
    assert result['sha256_verified']
    assert result['Mbps'] > 0


def test_transfer_timeout():
    with pytest.raises(RuntimeError):
        measure([sys.executable, '-c', 'import time; time.sleep(10)'], 'download', 100, .1)


def test_corrupt_download():
    with pytest.raises(RuntimeError, match='verification'):
        measure([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x"*100)'], 'download', 100, 5)


def test_node_speed_test_command(monkeypatch, capsys):
    from scripts import swarm_node, swarm_speed_test
    monkeypatch.setattr(swarm_speed_test, 'run', lambda peer, size, rounds: {'peer': peer, 'bytes': size, 'rounds': rounds})
    swarm_node.main(['speed-test', '--node', 'worker.local', '--bytes', '100', '--rounds', '1'])
    import json
    assert json.loads(capsys.readouterr().out) == {'peer': 'worker.local', 'bytes': 100, 'rounds': 1}


@pytest.mark.parametrize('direction', ['upload', 'download'])
def test_direct_tcp(direction):
    from pathlib import Path
    from scripts import swarm_speed_test
    source = Path(swarm_speed_test.__file__).read_text()
    result = swarm_speed_test.measure_tcp(['sh', '-c'], source, '127.0.0.1', direction, 300001, 5)
    assert result['sha256_verified']
    assert result['bytes'] == 300001
    assert result['Mbps'] > 0


def test_mptcp_negotiation():
    from pathlib import Path
    from scripts import swarm_speed_test
    try:
        probe = swarm_speed_test.data_socket(True)
    except (OSError, RuntimeError) as error:
        pytest.skip(f'Host MPTCP unavailable: {error}')
    probe.close()
    result = swarm_speed_test.measure_tcp(['sh', '-c'], Path(swarm_speed_test.__file__).read_text(),
                                         '127.0.0.1', 'upload', 300001, 5, multipath=True)
    assert result['sha256_verified']
    assert result['mptcp']['negotiated']
    assert not result['mptcp']['tcp_fallback']


def test_mptcp_info_flags():
    import struct
    from scripts.swarm_speed_test import multipath_info
    class Connection:
        def getsockopt(self, level, option, size):
            assert (level, option) == (284, 1)
            info = bytearray(88)
            struct.pack_into('=I', info, 8, 1)
            info[80] = 1
            return info
    assert multipath_info(Connection()) == {'negotiated': False, 'tcp_fallback': True,
                                            'additional_subflows': 0, 'subflows_total': 1}
