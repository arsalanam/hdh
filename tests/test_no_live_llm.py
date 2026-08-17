"""The guard that keeps `just qa` free and offline.

Every comprehension test injects a stub extractor, and the eval harness
runs from the CLI on demand — but nothing enforced that. One test calling
`llm_extractor()` would have started billing on every qa run and failed
in CI, which has no key. This pins the guard itself.
"""

import pytest


def test_constructing_a_real_client_fails_loudly():
    """The failure names the fix, so the next person doesn't have to guess."""
    anthropic = pytest.importorskip("anthropic")
    with pytest.raises(AssertionError) as err:
        anthropic.Anthropic(api_key="not-a-real-key")
    message = str(err.value)
    assert "billable API call" in message
    assert "stub_extractor" in message and "@pytest.mark.llm" in message


def test_the_llm_extractor_factory_is_blocked_too():
    """The factory builds its own client, so it is covered by the same
    guard — this is the call a future eval test would actually make."""
    pytest.importorskip("anthropic")
    from hdh.modules.comprehension.extract import llm_extractor

    with pytest.raises(AssertionError, match="billable API call"):
        llm_extractor()


def test_an_injected_client_still_works():
    """The guard blocks live clients, never dependency injection — the
    stub path every comprehension test relies on must stay open."""
    from hdh.modules.comprehension.extract import llm_extractor

    class _FakeClient:
        class beta:  # noqa: N801 - mirrors the SDK's attribute shape
            class messages:  # noqa: N801
                @staticmethod
                def create(**_kwargs):
                    raise AssertionError("not called in this test")

    extractor = llm_extractor(client=_FakeClient())
    assert callable(extractor)
