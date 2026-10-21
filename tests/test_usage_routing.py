"""Spending the last of the week's quota on the work that needs it.

Running the weekly Claude window out stops Jarvis answering at all until it
resets, which can be a day away. So as that window fills, spoken replies that
need no tools move to the NVIDIA models and the remaining quota is kept for
the turns only Claude can serve.

The line that must not be crossed: a turn with tools attached stays on Claude
whatever the pressure. A tool call routed to a model that cannot make one
turns a working answer into a wrong one, and a plainer voice is a far better
trade than a wrong action on his machine.
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
def _nim_is_configured(monkeypatch):
    monkeypatch.setattr(bridge, "_nim_conf", lambda: {
        "keys": [{"key": "not-a-real-key"}],
        "routing": {"enabled": True, "fast": "fast-model", "bg": "bg-model",
                    "classification": "class-model"},
    })
    # A rescue in one test marks the window spent for fifteen minutes; the
    # next test would then see every reply routed to NIM for no reason it
    # set up itself.
    bridge._spent["until"] = 0.0
    yield
    bridge._spent["until"] = 0.0


def at(week_percent):
    return {"ok": True, "week_utilization": week_percent}


@pytest.mark.unit
class TestReadingThePressure:
    def test_it_reports_the_weekly_window(self, monkeypatch):
        monkeypatch.setitem(bridge._usage_cache, "data", at(95.0))
        assert bridge.usage_pressure() == 95.0

    def test_a_refused_lookup_falls_back_to_the_last_real_figure(self, monkeypatch):
        # Anthropic rate-limits this lookup, and a refusal read as "nothing
        # used" -- switching the protection off at the moment the window was
        # tight enough for the lookup to matter.
        monkeypatch.setitem(bridge._usage_cache, "data", {"ok": False})
        monkeypatch.setitem(bridge._usage_cache, "good", at(96.0))
        monkeypatch.setitem(bridge._usage_cache, "good_t", time.time() - 60)
        assert bridge.usage_pressure() == 96.0

    def test_a_stale_figure_is_not_trusted_for_ever(self, monkeypatch):
        monkeypatch.setitem(bridge._usage_cache, "data", {"ok": False})
        monkeypatch.setitem(bridge._usage_cache, "good", at(96.0))
        monkeypatch.setitem(bridge._usage_cache, "good_t",
                            time.time() - bridge.USAGE_STALE_LIMIT_S - 60)
        assert bridge.usage_pressure() == 0.0

    def test_never_having_read_it_is_no_pressure(self, monkeypatch):
        # With nothing ever measured, guessing high would push every reply
        # onto NIM on a fresh start.
        monkeypatch.setitem(bridge._usage_cache, "data", {"ok": False})
        monkeypatch.setitem(bridge._usage_cache, "good", None)
        assert bridge.usage_pressure() == 0.0


@pytest.mark.unit
class TestWhereAReplyIsAnswered:
    def test_a_comfortable_week_keeps_his_voice_on_claude(self):
        assert bridge._nim_model_for("chat", False, tools=False, pressure=40.0) is None

    def test_a_nearly_empty_week_moves_plain_replies_to_nim(self):
        assert bridge._nim_model_for("chat", False, tools=False, pressure=95.0)

    def test_a_turn_with_tools_stays_on_claude_while_there_is_room(self):
        # A tool call routed to a weaker model is a wrong action on his
        # machine rather than a plainer sentence, so it is not worth doing
        # while any real quota remains.
        assert bridge._nim_model_for("chat", False, tools=True, pressure=92.0) is None

    def test_at_the_critical_mark_even_tool_turns_move(self):
        # The last of the window is worth more than the quality of any single
        # reply, and NIM drives the daemon's tools well enough: the protocol
        # is plain text, and the rescue path already answers this way.
        assert bridge._nim_model_for("chat", False, tools=True,
                                     pressure=bridge.WEEK_CRITICAL_AT + 1)

    def test_background_work_is_always_off_claude(self):
        for mode in ("fast", "bg"):
            assert bridge._nim_model_for(mode, False, pressure=0.0)

    def test_classification_is_always_off_claude(self):
        assert bridge._nim_model_for("chat", True, pressure=0.0)

    def test_agent_mode_is_never_diverted(self):
        # Agent mode is Claude Code driving his machine; NIM cannot do it.
        assert bridge._nim_model_for("agent", False, tools=False, pressure=99.0) is None

    def test_nothing_moves_when_nim_is_not_configured(self, monkeypatch):
        monkeypatch.setattr(bridge, "_nim_conf", lambda: {"keys": [], "routing": {}})
        assert bridge._nim_model_for("chat", False, tools=False, pressure=99.0) is None


@pytest.mark.unit
class TestWhenTheSubscriptionIsSpent:
    """The free models are the safety net, not just the errand boy.

    He assumed that running out of Claude did not stop Jarvis. It did: only
    background work went to NIM, so a spent window meant an assistant that
    said "I've reached the usage limit" and nothing else.
    """

    @pytest.mark.parametrize("message", [
        "Error: usage limit reached for this window",
        "429 Too Many Requests",
        "rate limit exceeded",
        "You are out of credits",
    ])
    def test_a_spent_window_is_answered_by_nim_instead(self, message):
        assert bridge._rescue_model(RuntimeError(message), "chat")

    @pytest.mark.parametrize("message", [
        "worker died",
        "timed out after 180s",
        "not authenticated, run claude login",
        "error_max_turns",
    ])
    def test_every_other_failure_still_surfaces(self, message):
        # Answering a crashed worker with a different model hides the fault
        # and makes the next one harder to find.
        assert bridge._rescue_model(RuntimeError(message), "chat") is None

    def test_no_rescue_without_keys(self, monkeypatch):
        monkeypatch.setattr(bridge, "_nim_conf", lambda: {"keys": [], "routing": {}})
        assert bridge._rescue_model(RuntimeError("usage limit"), "chat") is None


@pytest.mark.unit
class TestWhichLimitHeIsToldAbout:
    def _warning(self, monkeypatch, session, week):
        monkeypatch.setitem(bridge._usage_cache, "data", {
            "ok": True,
            "session_utilization": session, "session_reset": "2026-08-12 15:20:00+00:00",
            "week_utilization": week, "week_reset": "2026-08-13 14:00:00+00:00",
        })
        bridge._usage_warned.update(reset=None, hit=set())
        return bridge._usage_warning()

    def test_the_emptier_window_is_the_one_he_hears_about(self, monkeypatch):
        # Warning about a session at 35% while the week sits at 95% tells him
        # the comfortable half of the truth.
        warning = self._warning(monkeypatch, session=35.0, week=95.0)
        assert "95%" in warning and "weekly" in warning

    def test_a_busy_session_still_gets_mentioned(self, monkeypatch):
        warning = self._warning(monkeypatch, session=92.0, week=40.0)
        assert "92%" in warning and "five-hour" in warning

    def test_a_comfortable_pair_says_nothing(self, monkeypatch):
        assert self._warning(monkeypatch, session=10.0, week=12.0) == ""

    def test_it_does_not_threaten_to_stop(self, monkeypatch):
        # It used to say he stops working entirely when the window runs out.
        # That stopped being true when the NVIDIA models started answering,
        # and a warning about a consequence that no longer exists is worse
        # than none: he acts on it.
        warning = self._warning(monkeypatch, session=35.0, week=95.0).lower()
        assert "nvidia" in warning
        assert "do not stop" in warning or "keep working" in warning


@pytest.mark.unit
class TestTheCLIsActualWording:
    """The refusal the CLI really sends, verbatim from the bridge log.

    Four turns in a row died on "You've hit your session limit · resets
    12pm" — a wording the quota pattern did not know, so no rescue ever
    fired and the user concluded the NVIDIA keys were dead. The keys were
    fine; the regex was deaf.
    """

    @pytest.mark.parametrize("message", [
        "claude CLI error: You've hit your session limit · resets 12pm (Europe/Athens)",
        "claude CLI error: You've hit your weekly limit · resets 5pm (Europe/Athens)",
    ])
    def test_the_real_refusal_is_rescued(self, message):
        assert bridge._rescue_model(RuntimeError(message), "chat")

    @pytest.mark.parametrize("message", [
        "claude CLI error: You've hit your session limit · resets 12pm (Europe/Athens)",
    ])
    def test_the_real_refusal_is_named_for_what_it_is(self, message):
        spoken = bridge._spoken_failure(RuntimeError(message))
        assert "window is spent" in spoken.lower() or "fallback" in spoken.lower()


@pytest.mark.unit
class TestRememberingASpentWindow:
    """One refusal is a fact about every request for the next while.

    Without the memory, each turn pays a failed CLI round-trip before its
    rescue — slow, and each failure risks being the one that surfaces.
    """

    def test_a_rescue_marks_the_window_spent(self):
        bridge._rescue_model(RuntimeError("You've hit your session limit"), "chat")
        assert bridge._window_spent()

    def test_while_spent_every_chat_turn_goes_to_nim(self):
        bridge._note_spent()
        assert bridge._nim_model_for("chat", False, tools=False, pressure=0.0)

    def test_while_spent_even_tool_turns_go_to_nim(self):
        bridge._note_spent()
        assert bridge._nim_model_for("chat", False, tools=True, pressure=0.0)

    def test_the_memory_expires(self, monkeypatch):
        bridge._note_spent()
        monkeypatch.setattr(bridge.time, "time",
                            lambda: bridge._spent["until"] + 1)
        assert not bridge._window_spent()

    def test_a_non_quota_failure_does_not_mark_it(self):
        bridge._rescue_model(RuntimeError("worker died"), "chat")
        assert not bridge._window_spent()


@pytest.mark.unit
class TestAShortQuestionIsNotASmallOne:
    """"Read me the overnight review" is six words and a file tool.

    It looked simple, so it went to the fast lane; the fast lane goes to a
    small model; and a small model emulating a tool call there answered as
    though it had read the file without ever calling anything. He asked, and
    nothing was read.
    """

    def test_a_tool_turn_in_the_fast_lane_stays_on_claude(self):
        assert bridge._nim_model_for("fast", False, tools=True,
                                     pressure=20.0) is None

    def test_tool_free_fast_work_still_goes_to_nim(self):
        # Classification and the like: no tools, no risk, and the whole
        # reason the cheap lane exists.
        assert bridge._nim_model_for("fast", False, tools=False, pressure=20.0)

    def test_at_the_critical_mark_even_fast_tool_turns_move(self):
        # A plainer answer beats no answer once the window is nearly gone.
        assert bridge._nim_model_for("fast", False, tools=True,
                                     pressure=bridge.WEEK_CRITICAL_AT + 1)

    def test_a_spent_window_moves_them_too(self):
        bridge._note_spent()
        assert bridge._nim_model_for("fast", False, tools=True, pressure=0.0)

    def test_background_work_is_untouched(self):
        assert bridge._nim_model_for("bg", False, tools=True, pressure=0.0)
