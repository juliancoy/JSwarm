"""Display-name continuity by MAC; cached names never establish connectivity."""
from datetime import datetime,timezone

NAME_TTL_SECONDS=24*60*60


def stamp(value):
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError,TypeError,AttributeError):return None


def retain_device_names(devices,records,observed_at):
    now=stamp(observed_at)
    known={str(record.get('mac') or '').lower():record for record in records}
    result=[]
    for device in devices:
        row=dict(device)
        if row.get('hostname'):
            row.update(hostname_source='local' if row.get('self') else 'mdns',hostname_last_seen=observed_at)
        elif now and device.get('mac'):
            candidates=[]
            for name in known.get(str(device['mac']).lower(),{}).get('names',[]):
                seen=stamp(name.get('last_seen'))
                if (name.get('source') in ('Ethernet discovery','Saved Ethernet snapshot') and
                    str(name.get('name') or '').lower().endswith('.local') and seen and
                    0<=(now-seen).total_seconds()<=NAME_TTL_SECONDS):
                    candidates.append((seen,name['name'],name['last_seen']))
            if candidates:
                _,hostname,seen=max(candidates)
                row.update(hostname=hostname,hostname_source='cached_mdns',hostname_last_seen=seen)
        result.append(row)
    return result
