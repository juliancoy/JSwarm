"""LAN-first Docker image, build and container utilities."""
from .images import ImageResolver
from .policy import build_image, ensure_image, prepare_build, inventory, ssh

__version__='0.1.0'
_SDK={'from_env','ensure_network','run_container','remove_container','list_containers','wait_for_db','wait_for_port'}


def __getattr__(name):
    if name=='DOCKER_CLIENT':
        from .sdk import client
        return client()
    if name in _SDK:
        from . import sdk
        return getattr(sdk,name)
    raise AttributeError(name)
