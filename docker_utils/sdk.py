"""Docker SDK client whose implicit pulls also follow the shared image policy."""
from pathlib import Path
import time
from urllib.parse import urlsplit

import docker
from docker.models.images import ImageCollection

from .policy import ensure_image, prepare_build, build_image


class LANImages(ImageCollection):
    def pull(self, repository, tag=None, all_tags=False, **kwargs):
        if kwargs.get('platform'):raise ValueError('Explicit platform pulls are not supported by the native LAN cache policy')
        if all_tags or any(value for key,value in kwargs.items() if key!='platform'):
            raise ValueError('LAN-first pulls require a single image reference and Docker CLI credential configuration')
        image=repository+(':'+tag if tag else '')
        return self.get(ensure_image(image))

    def build(self, **kwargs):
        options=dict(kwargs)
        if options.get('custom_context') or options.get('remote') or options.get('platform') or options.get('fileobj'):
            raise ValueError('LAN-first builds require a local Dockerfile context')
        path=Path(options.get('path','.'))/options.get('dockerfile','Dockerfile')
        prepared=prepare_build(path.read_text(),options.get('buildargs'))
        ignore=Path(options.get('path','.'))/'.dockerignore'
        exclude=ignore.read_text().splitlines() if ignore.exists() else []
        context=docker.utils.tar(options.get('path','.'),exclude=exclude,
                                 dockerfile=('.docker-utils.Dockerfile',prepared))
        options.update(fileobj=context,custom_context=True,dockerfile='.docker-utils.Dockerfile',pull=False)
        options.pop('path',None)
        try:return super().build(**options)
        finally:context.close()



class LANClient(docker.DockerClient):
    @property
    def images(self):return LANImages(client=self)


def from_env(**kwargs):
    return LANClient.from_env(**kwargs)


_client=None


def client():
    global _client
    if _client is None:_client=from_env()
    return _client


def ensure_network(name):
    try:return client().networks.get(name)
    except docker.errors.NotFound:return client().networks.create(name)


def remove_container(name):
    try:client().containers.get(name).remove(force=True)
    except docker.errors.NotFound:pass


def run_container(config):
    options=dict(config)
    build=options.pop('build',None)
    rebuild=options.pop('rebuild_image_on_restart',False)
    name=options.get('name')
    if name:
        try:
            existing=client().containers.get(name)
            if existing.status=='running':return existing
            existing.remove(force=True)
        except docker.errors.NotFound:pass
    if build and rebuild:
        build_image(options['image'],build['context'],build.get('dockerfile','Dockerfile'),build.get('buildargs'))
    options['image']=ensure_image(options['image'])
    return client().containers.run(**options)


def list_containers(show_all=False):
    rows=['CONTAINER ID\tIMAGE\tSTATUS\tNAMES']
    for container in client().containers.list(all=show_all):
        rows.append(f'{container.short_id}\t{container.image.id}\t{container.status}\t{container.name}')
    return '\n'.join(rows)


def wait_for_port(host,port,network,retries=60,delay=2):
    image=ensure_image('busybox:latest')
    script='i=0; while [ "$i" -lt "$ATTEMPTS" ]; do nc -z -w 2 "$TARGET" "$PORT" && exit 0; i=$((i+1)); sleep "$DELAY"; done; exit 1'
    return client().containers.run(image,['sh','-c',script],network=network,remove=True,
        environment={'TARGET':host,'PORT':str(port),'ATTEMPTS':str(retries),'DELAY':str(delay)})


def wait_for_db(network,db_url,db_user='postgres',max_attempts=30,delay=2):
    parsed=urlsplit(db_url if '://' in db_url else 'postgresql://'+db_url)
    if not parsed.hostname:raise ValueError('Database URL must contain a hostname')
    image=ensure_image('postgres:15-alpine')
    for attempt in range(max_attempts):
        try:
            client().containers.run(image,['pg_isready','-h',parsed.hostname,'-p',str(parsed.port or 5432),'-U',db_user],
                network=network,remove=True)
            return
        except docker.errors.ContainerError:
            if attempt+1<max_attempts:time.sleep(delay)
    raise RuntimeError('Database readiness timed out')
