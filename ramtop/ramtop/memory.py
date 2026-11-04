"""Display-ready memory telemetry; cache events are never interpreted as DRAM bytes."""
from .hardware import read


def cache_rates(samples):
    result = {}
    for alias in ('cache references', 'cache misses'):
        values = [value for event, value in samples
                  if event.tier == 4 and event.unit == 'events/s' and event.label.endswith('/' + alias)]
        result[alias] = sum(values) if values and all(value is not None for value in values) else None
    references, misses = result.values()
    result['miss ratio'] = misses / references * 100 if (references is not None and references > 0
                                                        and misses is not None and misses <= references) else None
    return result


def mbm_rates(samples):
    result = {}
    for alias in ('mbm_total_bytes', 'mbm_local_bytes'):
        values = [value for event, value in samples
                  if event.tier == 3 and event.unit == 'B/s' and event.label.endswith('/' + alias)]
        result[alias] = sum(values) if values and all(value is not None for value in values) else None
    return result


def pressure(text=None):
    result = {}
    for line in (read('/proc/pressure/memory') if text is None else text).splitlines():
        fields = line.split()
        try:
            result[fields[0]] = float(dict(field.split('=', 1) for field in fields[1:])['avg10'])
        except (ValueError, IndexError, KeyError):
            continue
    return result


def memory_fields(text=None):
    fields = {}
    for line in (read('/proc/meminfo') if text is None else text).splitlines():
        key, _, value = line.partition(':')
        parts = value.split()
        if parts and parts[0].isdigit():
            fields[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == 'kB' else 1)
    return fields
