"""A fallback with one model in it is not a fallback.

NVIDIA takes hosted models out of service without warning and leaves them
listed: the model that had answered every background call for days started
returning "DEGRADED function cannot be invoked", and with nothing behind it
the safety net that was supposed to keep Jarvis alive went down with it.

So a lane is a list, tried in order, and a model NVIDIA is not serving is set
aside rather than retried into the ground.
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_bridge", ROOT / "claude-bridge.py")
bridge = importlib.util.module_from_spec(spec)
sys.modules["claude_bridge"] = bridge
spec.loader.exec_module(bridge)


@pytest.fixture(autouse=True)
def _clean_health():
    bridge._nim_sidelined.clear()
    yield
    bridge._nim_sidelined.clear()


class Answer:
    def __init__(self, text="ok"):
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self.text}}]}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.mark.unit
class TestTryingTheNextModel:
    def test_one_name_still_works(self, monkeypatch):
        monkeypatch.setattr(bridge, "_nim_request",
                            lambda model, *a, **k: Answer(f"from {model}"))
        assert bridge._nim_chat("only-model", "", "", timeout=1) == "from only-model"

    def test_a_dead_model_is_stepped_over(self, monkeypatch):
        tried = []

        def request(model, *args, **kwargs):
            tried.append(model)
            if model == "degraded":
                raise RuntimeError("NIM HTTP 400: DEGRADED function cannot be invoked")
            return Answer(f"from {model}")

        monkeypatch.setattr(bridge, "_nim_request", request)
        assert bridge._nim_chat(["degraded", "healthy"], "", "", timeout=1) == "from healthy"
        assert tried == ["degraded", "healthy"]

    def test_a_model_answering_with_nothing_is_stepped_over(self, monkeypatch):
        # An empty reply breaks the daemon's contract more quietly than an
        # error, so it counts as a failure and the next model gets a turn.
        monkeypatch.setattr(bridge, "_nim_request",
                            lambda model, *a, **k: Answer("" if model == "mute" else "spoke"))
        assert bridge._nim_chat(["mute", "talker"], "", "", timeout=1) == "spoke"

    def test_all_of_them_failing_surfaces_the_failure(self, monkeypatch):
        monkeypatch.setattr(bridge, "_nim_request",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
        with pytest.raises(RuntimeError):
            bridge._nim_chat(["one", "two"], "", "", timeout=1)


@pytest.mark.unit
class TestSettingABrokenModelAside:
    @pytest.mark.parametrize("status,detail", [
        (400, "Function id 'abc': DEGRADED function cannot be invoked"),
        (404, "404 page not found"),
        (410, "The model 'x' is no longer available"),
    ])
    def test_the_model_itself_being_gone_is_recognised(self, status, detail):
        assert bridge._model_is_broken(status, detail)

    @pytest.mark.parametrize("status,detail", [
        (401, "unauthorized"),
        (403, "the bound service account is deleted or disabled"),
    ])
    def test_a_refused_key_is_set_aside_too(self, status, detail):
        # Authentication does not recover between one request and the next.
        # His Gemini key died overnight and every turn paid two rejections
        # before falling through, because a 401 was read as bad luck.
        assert bridge._model_is_broken(status, detail)

    @pytest.mark.parametrize("status,detail", [
        (400, "invalid value for max_tokens"),
        (500, "internal error"),
        (503, "service temporarily unavailable"),
    ])
    def test_other_failures_are_not_blamed_on_the_model(self, status, detail):
        # A malformed request is ours and an outage is theirs. Both recover on
        # their own, and sidelining a working model for either would shrink the
        # fallback that is meant to be keeping the assistant alive.
        assert not bridge._model_is_broken(status, detail)

    def test_a_sidelined_model_is_skipped(self, monkeypatch):
        tried = []
        monkeypatch.setattr(bridge, "_nim_request",
                            lambda model, *a, **k: tried.append(model) or Answer())
        bridge._nim_sideline("degraded", "DEGRADED")
        bridge._nim_chat(["degraded", "healthy"], "", "", timeout=1)
        assert tried == ["healthy"], "it retried a model NVIDIA is not serving"

    def test_it_comes_back_after_the_outage_window(self, monkeypatch):
        bridge._nim_sideline("degraded", "DEGRADED")
        assert not bridge._nim_healthy("degraded")
        bridge._nim_sidelined["degraded"] = time.time() - 1
        assert bridge._nim_healthy("degraded"), "an outage must clear itself"
