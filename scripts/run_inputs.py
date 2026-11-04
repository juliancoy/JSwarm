"""Run-manager input planning for bounded streams and seekable cached inputs."""
from dataclasses import dataclass, asdict

STREAMABLE = frozenset({'bam-read-qc'})

@dataclass(frozen=True)
class InputPlan:
    mode: str
    parallelism: int
    input_bytes: int
    cache_bytes_required: int
    buffer_bytes_per_stream: int
    reason: str

    def as_dict(self):return asdict(self)


def plan_inputs(jobs, mode='auto', parallelism=2, free_cache_bytes=0, available_cpus=4, available_memory_mb=4096):
    if mode not in ('auto','stream','cache'):raise ValueError('Choose auto, stream, or cache input mode')
    if not jobs or not 1<=parallelism<=8:raise ValueError('Expected jobs and bounded parallelism (1–8)')
    for j in jobs:
        if type(j['spec'].get('bytes')) is not int or j['spec']['bytes']<=0:raise ValueError('Invalid input size')
    streamable=all(j['spec']['kind'] in STREAMABLE and j.get('input_access','sequential')=='sequential' for j in jobs)
    selected='stream' if mode=='stream' or mode=='auto' and streamable else 'cache'
    if selected=='stream' and not streamable:raise ValueError('This recipe requires seekable input; streaming is unavailable')
    count=min(parallelism,len(jobs),available_cpus//2,available_memory_mb//2048)
    if count<1:raise ValueError('No admitted CPU/RAM slot is currently available')
    total=sum(j['spec']['bytes'] for j in jobs);required=0 if selected=='stream' else total
    if required and free_cache_bytes<required+1024**3:raise ValueError('Insufficient cache space; use a supported sequential stream or another worker')
    return InputPlan(selected,count,total,required,1024**2 if selected=='stream' else 0,
                     'Sequential recipe; parse while receiving, verify full input before publishing' if selected=='stream' else 'Seekable input cache required')
