import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from daemon.radio_browser import search_stations  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_opener(payload):
    def opener(request, timeout=5.0):
        return FakeResponse(payload)
    return opener


def test_empty_query_returns_empty_without_network_call():
    called = {"count": 0}

    def opener(request, timeout=5.0):
        called["count"] += 1
        return FakeResponse([])

    assert search_stations("   ", opener=opener) == []
    assert called["count"] == 0


def test_search_maps_fields_and_prefers_resolved_url():
    payload = [
        {
            "stationuuid": "abc-123",
            "name": "Test Radio",
            "url": "http://example.com/raw",
            "url_resolved": "http://example.com/resolved",
            "country": "Germany",
            "tags": "pop,talk",
            "bitrate": 128,
            "favicon": "http://example.com/icon.png",
        }
    ]
    results = search_stations("test", opener=fake_opener(payload))
    assert len(results) == 1
    r = results[0]
    assert r["stationuuid"] == "abc-123"
    assert r["name"] == "Test Radio"
    assert r["url"] == "http://example.com/resolved"  # resolved preferred over raw
    assert r["country"] == "Germany"
    assert r["bitrate"] == 128


def test_search_drops_results_with_no_usable_url():
    payload = [{"stationuuid": "x", "name": "No URL Station", "url": "", "url_resolved": ""}]
    results = search_stations("test", opener=fake_opener(payload))
    assert results == []


def test_search_survives_network_failure():
    def broken_opener(request, timeout=5.0):
        raise OSError("network is down")

    assert search_stations("test", opener=broken_opener) == []


def test_search_survives_bad_json():
    class BadResponse:
        def read(self):
            return b"not json"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(request, timeout=5.0):
        return BadResponse()

    assert search_stations("test", opener=opener) == []


if __name__ == "__main__":
    import inspect

    module = sys.modules[__name__]
    tests = [obj for name, obj in vars(module).items() if name.startswith("test_") and inspect.isfunction(obj)]
    for test in tests:
        test()
        print(f"OK  {test.__name__}")
    print(f"\n{len(tests)} tests passed")
