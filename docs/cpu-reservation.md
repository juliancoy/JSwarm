# Logical CPU reservation

Swarm nodes default to `SWARM_RESERVED_CPUS=1`. The scheduler advertises the
remaining CPUs, and managed stream job containers use a Docker CPU affinity mask
that excludes the lowest N IDs in the node agent's allowed CPU set. CPU time
sharing continues within that mask; reserving capacity alone would not protect
host responsiveness under oversubscription. CPU and GPU job containers share
the same worker mask. Host services and the agent remain unrestricted.

Set `SWARM_RESERVED_CPUS=2` in a native node service environment, or pass
`--reserved-cpus 2` when rendering its standard container service with
`scripts/agent_container.py`. Use `0` to disable the reserve. Invalid and negative
values are rejected. Reserving all available CPUs advertises zero worker CPU
capacity and prevents new managed stream containers from launching.

This counts logical CPUs, not physical cores or hyperthread sibling groups.
It does not isolate CPUs from unrelated applications. Existing job containers
retain their current masks until replaced; changing the setting does not stop
or restart work in progress.
