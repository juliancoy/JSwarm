import concurrent.futures
import gzip
import json
import threading
import time
from unittest.mock import patch
from urllib.request import Request, urlopen

import pytest

from observation_feed import ObservationFeed, SnapshotWarming, accepts_gzip


def published(feed):
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        with feed.lock:
            if not feed.busy:
                return
        time.sleep(.005)
    raise AssertionError('Refresh did not finish')


def test_slow_refresh_never_blocks_readers_and_is_single_flight():
    entered, release = threading.Event(), threading.Event()
    calls = []
    def produce():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return {'sampled_at': 1, 'items': ['original']}
    feed = ObservationFeed(produce, interval=0)
    feed.refresh()
    assert entered.wait(1)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(feed.get) for _ in range(16)]
            for future in futures:
                with pytest.raises(SnapshotWarming):
                    future.result(timeout=.5)
        assert len(calls) == 1
    finally:
        release.set()
    published(feed)
    entered.clear();release.clear()
    try:
        revision, sample = feed.get()
        assert entered.wait(1)
        assert revision == 1 and sample['sampled_at'] == 1
        # A refresh is in flight; all other clients share the previous sample.
        assert feed.get() == (revision, sample)
        assert len(calls) == 2
    finally:
        release.set()
    published(feed)


def test_refresh_failure_retains_timestamp_and_compressed_payload():
    feed = ObservationFeed(lambda: {'sampled_at': 123, 'items': ['x'*1000]}, interval=0)
    feed.refresh();published(feed)
    body, compressed, observed, error = feed.response('gzip')
    assert compressed and json.loads(gzip.decompress(body))['sampled_at'] == observed == 123
    published(feed)
    def fail():
        raise RuntimeError('Unavailable source')
    feed.producer = fail
    feed.refresh();published(feed)
    # Avoid triggering another attempt while examining the retained response.
    feed.interval = 100
    retained, compressed, observed, error = feed.response('gzip')
    assert retained == body and observed == 123 and error


@pytest.mark.parametrize('header,expected', [('gzip', True), ('br, gzip;q=0.5', True),
    ('gzip;q=0', False), ('gzip;q=invalid', False), ('br', False), ('', False)])
def test_encoding_negotiation(header, expected):
    assert accepts_gzip(header) is expected


def test_http_serves_cached_encoding_and_explicit_observation_age():
    import task_dashboard
    feed = ObservationFeed(lambda: {'sampled_at': time.time()-10, 'items': ['x'*1000]}, interval=100)
    feed.refresh();published(feed)
    server = task_dashboard.ThreadingHTTPServer(('127.0.0.1', 0), task_dashboard.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(task_dashboard, 'pipeline_feed', feed):
            endpoint = f'http://127.0.0.1:{server.server_port}/api/tasks/pipeline'
            with urlopen(Request(endpoint, headers={'Accept-Encoding': 'gzip'}), timeout=1) as response:
                assert response.headers['Content-Encoding'] == 'gzip'
                assert response.headers['Vary'] == 'Accept-Encoding'
                assert float(response.headers['X-Observation-Age']) >= 10
                compressed = response.read()
            with urlopen(Request(endpoint, headers={'Accept-Encoding': 'gzip;q=0'}), timeout=1) as response:
                assert response.headers['Content-Encoding'] is None
                plain = response.read()
            assert gzip.decompress(compressed) == plain and len(compressed) < len(plain)
    finally:
        server.shutdown();server.server_close();thread.join(1)
