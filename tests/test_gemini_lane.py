"""Gemini models in the fallback lanes, served by Google instead of NVIDIA.

A lane is still one ordered list. A model whose name starts with "gemini"
goes to Google's OpenAI-compatible endpoint with the Gemini keys; everything
else in the same list still goes to NVIDIA. The lane machinery — rotation,
sidelining, next-model-on-failure — neither knows nor cares which is which,
so a Gemini outage falls through to NVIDIA and vice versa.

A Gemini model with no key configured is absent, not broken: it is skipped
silently rather than logging a failure on every turn.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_bridge", ROOT / "claude-bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules["claude_bridge"] = bridge
spec.loader.exec_module(bridge)


def conf(gemini_keys):
    return lambda: {
        "keys": [{"key": "nvidia-key"}],
        "gemini_keys": gemini_keys,
        "routing": {"enabled": True},
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    bridge._nim_sidelined.clear()
    yield
    bridge._nim_sidelined.clear()


class Captured:
    status_code = 200

    def __init__(self):
        self.url = None
        self.headers = None
        self.payload = None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.mark.unit
class TestWhichProviderServes:
    def _post(self, monkeypatch, keys):
        seen = Captured()

        def post(url, headers=None, json=None, **_):
            seen.url, seen.headers, seen.payload = url, headers, json
            return seen

        monkeypatch.setattr(bridge, "_nim_conf", conf(keys))
        monkeypatch.setattr(bridge.requests, "post", post)
        return seen

    def test_a_gemini_model_goes_to_google_with_the_gemini_key(self, monkeypatch):
        seen = self._post(monkeypatch, ["gem-key"])
        bridge._nim_chat("gemini-2.5-flash", "sys", "hello", timeout=5)
        assert seen.url.startswith(bridge.GEMINI_BASE)
        assert seen.headers["Authorization"] == "Bearer gem-key"

    def test_an_nvidia_model_still_goes_to_nvidia(self, monkeypatch):
        seen = self._post(monkeypatch, ["gem-key"])
        bridge._nim_chat("meta/llama-3.3-70b-instruct", "sys", "hello", timeout=5)
        assert "nvidia" in seen.url
        assert seen.headers["Authorization"] == "Bearer nvidia-key"

    def test_gemini_is_asked_not_to_sit_and_think(self, monkeypatch):
        # Gemini 2.5 reasons by default; on a voice reply that is latency
        # with nothing to show for it.
        seen = self._post(monkeypatch, ["gem-key"])
        bridge._nim_chat("gemini-2.5-flash", "sys", "hello", timeout=5)
        assert seen.payload.get("reasoning_effort") == "low"
        assert "chat_template_kwargs" not in seen.payload

    def test_nvidia_keeps_its_own_thinking_switch(self, monkeypatch):
        seen = self._post(monkeypatch, ["gem-key"])
        bridge._nim_chat("meta/llama-3.3-70b-instruct", "sys", "hello", timeout=5)
        assert seen.payload["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.unit
class TestAKeylessGeminiIsAbsentNotBroken:
    def test_it_is_skipped_without_a_key(self, monkeypatch):
        monkeypatch.setattr(bridge, "_nim_conf", conf([]))
        assert bridge._nim_healthy("gemini-2.5-flash") is False

    def test_it_is_healthy_with_a_key(self, monkeypatch):
        monkeypatch.setattr(bridge, "_nim_conf", conf(["gem-key"]))
        assert bridge._nim_healthy("gemini-2.5-flash") is True

    def test_the_lane_falls_through_to_nvidia(self, monkeypatch):
        # The chat lane leads with Gemini; with no key the same call must
        # still be answered, by the NVIDIA models behind it.
        monkeypatch.setattr(bridge, "_nim_conf", conf([]))
        seen = Captured()

        def post(url, headers=None, json=None, **_):
            seen.url, seen.headers, seen.payload = url, headers, json
            return seen

        monkeypatch.setattr(bridge.requests, "post", post)
        text = bridge._nim_chat(["gemini-2.5-flash", "meta/llama-3.1-8b-instruct"],
                                "sys", "hello", timeout=5)
        assert text == "ok"
        assert "nvidia" in seen.url

    def test_a_dict_shaped_key_works_too(self, monkeypatch):
        monkeypatch.setattr(bridge, "_nim_conf",
                            conf([{"name": "main", "key": "gem-key"}]))
        assert bridge._gemini_keys() == ["gem-key"]


@pytest.mark.unit
class TestAKeyThatStopsWorking:
    """His Gemini key was disabled overnight, mid-sentence.

    A refused key is not this request's bad luck: it will refuse the next one
    too. Retrying it on every call cost two rejections before every fallback.
    """

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_key_sets_the_model_aside(self, status):
        assert bridge._model_is_broken(status, "service account is disabled")

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_a_busy_or_broken_server_is_still_worth_retrying(self, status):
        assert not bridge._model_is_broken(status, "try again")

    def test_the_lane_falls_through_to_nvidia_on_a_dead_key(self, monkeypatch):
        calls = []

        def request(model, system_prompt, prompt, *, timeout, stream):
            calls.append(model)
            if model.startswith("gemini"):
                bridge._nim_sideline(model, "401 service account disabled")
                raise RuntimeError("NIM HTTP 401")

            class Answer:
                def json(self):
                    return {"choices": [{"message": {"content": "ok"}}]}

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

            return Answer()

        monkeypatch.setattr(bridge, "_nim_request", request)
        text = bridge._nim_chat(["gemini-flash-latest", "meta/llama-3.1-8b-instruct"],
                                "sys", "hi", timeout=5)
        assert text == "ok"
        # And the next call does not pay for the dead key again.
        calls.clear()
        bridge._nim_chat(["gemini-flash-latest", "meta/llama-3.1-8b-instruct"],
                         "sys", "hi", timeout=5)
        assert not any(m.startswith("gemini") for m in calls)
