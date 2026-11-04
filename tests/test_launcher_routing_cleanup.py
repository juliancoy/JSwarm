from unittest.mock import MagicMock,patch
import pytest
from scripts.launcher_registry import configure_launcher_routing


def test_rootless_engine_never_creates_host_routing_helpers():
    client=MagicMock();client.info.return_value={'SecurityOptions':['name=rootless']}
    with patch('docker.from_env',return_value=client):
        assert configure_launcher_routing({})['status']=='unavailable'
    client.containers.create.assert_not_called()
    client.close.assert_called_once()


def test_failed_helper_start_still_removes_created_container_and_closes_client():
    client=MagicMock();client.info.return_value={'SecurityOptions':[]}
    helper=client.containers.create.return_value
    helper.start.side_effect=RuntimeError('cannot start')
    with patch('docker.from_env',return_value=client):
        with pytest.raises(RuntimeError,match='cannot start'):configure_launcher_routing({})
    helper.remove.assert_called_once_with(force=True)
    client.close.assert_called_once()
