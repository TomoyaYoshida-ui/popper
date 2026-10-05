"""No-network regression checks for the bounded paid acceptance wrapper."""
from types import SimpleNamespace

import pytest

from popper.core import ProtocolError
from scripts.workbench_acceptance_server import LimitedClient


class FakeClient:
    descriptor = {}

    def __init__(self, usage):
        self.usage = usage
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(usage=self.usage)


def test_resumed_usage_preserves_three_request_limit():
    fake = FakeClient({"total_tokens": 100})
    client = LimitedClient(fake, calls=1, tokens=4913, token_limit=4_000_000)
    client("synthetic", [])
    client("synthetic", [])
    assert (client.calls, client.tokens) == (3, 5113)
    with pytest.raises(ProtocolError):
        client("synthetic", [])
    assert fake.calls == 2


def test_missing_usage_stops_even_with_increased_budget():
    fake = FakeClient({})
    client = LimitedClient(fake, calls=1, tokens=4913, token_limit=4_000_000)
    client("synthetic", [])
    with pytest.raises(ProtocolError):
        client("synthetic", [])
    assert fake.calls == 1


def test_consumed_token_limit_prevents_outbound_request():
    fake = FakeClient({"total_tokens": 100})
    client = LimitedClient(fake, tokens=4_000_000, token_limit=4_000_000)
    with pytest.raises(ProtocolError):
        client("synthetic", [])
    assert fake.calls == 0
