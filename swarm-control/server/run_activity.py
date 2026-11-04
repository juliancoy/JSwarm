"""Bounded semantic events captured with each persisted coordinator snapshot."""

def snapshot(run):
    rows = {}
    def add(key, value, message, **context):
        if value is not None:
            rows[key] = (value, {'message': message, **context})
    add('status', run.get('status'), 'Run ' + str(run.get('status')))
    add('preparation', run.get('preparation'), str(run.get('preparation')))
    add('error', run.get('error'), str(run.get('error')), level='error')
    image = run.get('image_progress') or {}
    if image:
        phase = image.get('phase', '')
        action = {'pulling': 'Download started', 'downloaded': 'Download completed', 'building': 'Image build started',
                  'publishing': 'Publishing image', 'distributing_layers': 'LAN layer transfer started',
                  'ready': 'Image ready', 'cached': 'Cached image available', 'failed': 'Image failed'}.get(phase, 'Image ' + phase.replace('_', ' '))
        add('image', tuple(image.get(k) for k in ('image', 'phase', 'source', 'destination')),
            action + ': ' + str(image.get('image', '')), source=image.get('source'), host=image.get('destination'))
    for name, repo in run.get('repositories', {}).items():
        add('repo:' + name, repo.get('stage'), name + ' · ' + str(repo.get('stage')))
    for host in run.get('hosts', []):
        name = host.get('host')
        if not name:continue
        session = host.get('session') or {}
        add('session:' + name, session.get('status'), 'Launcher ' + str(session.get('status')), host=name)
        if host.get('error'):
            add('host-error:' + name, host['error'], host['error'], host=name, level='error')
        for suite, job in host.get('jobs', {}).items():
            state = (job.get('status'), job.get('phase'))
            if state == ('queued', None):
                continue
            title = job.get('name', suite)
            message = title + ' · ' + str(state[0]) + (' · ' + str(state[1]) if state[1] else '')
            add('suite:' + name + ':' + suite, state, message, host=name, suite=suite,
                level='error' if state[0] in ('failed', 'unavailable', 'timed_out') else 'info')
    return rows


def capture(run, previous, timestamp):
    current = snapshot(run)
    events = run.setdefault('activity', [])
    for key, (value, event) in current.items():
        if previous.get(key, (None,))[0] != value:
            events.append({'time': timestamp, **event})
    run['activity'] = events[-300:]
    return current
