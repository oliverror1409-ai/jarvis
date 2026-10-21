"""The lane has to keep the tools that can do the job.

Agent mode swaps the daemon's tools for Claude Code's shell. A shell has no
screen, no keyboard on his desk, no speakers and no way to show him anything,
so any request that needs those comes back as a sentence claiming the work was
done. It has happened five times now, each with a different verb: browse,
type, show, read, play. Every one of those is pinned here.

The tools list is what the daemon actually attached, because a flag that
ignores it would strand a request in a lane with nothing to do the work.
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


def attached(*names):
    """The shape the daemon sends tools in."""
    return [{"type": "function", "function": {"name": n}} for n in names]


EVERYTHING = attached("browser", "inputControl", "windowControl", "showPanel",
                      "viewScreen", "driveApp", "playMusic", "localFiles",
                      "openFile", "findFiles")


def owned(text, tools=EVERYTHING):
    return {job for job, yes in bridge.daemon_owns(text, tools).items() if yes}


@pytest.mark.unit
class TestWorkOnlyTheDaemonCanDo:
    @pytest.mark.parametrize("said,job", [
        ("play my playlist Polonia from Apple Music", "music"),
        ("put on some music", "music"),
        ("play the Discovery album", "music"),
        ("what is in my calendar today", "sight"),
        ("what have I got on tomorrow", "sight"),
        ("read that for me", "sight"),
        ("type this into the chat box", "machine"),
        ("switch to the Canva window", "machine"),
        ("show me the figures on screen", "screen"),
        ("open the screwfix hotdogzone video", "files"),
        ("open that invoice from 2016", "files"),
        ("find my hot dog photo", "files"),
    ])
    def test_the_daemon_keeps_it(self, said, job):
        assert job in owned(said), f"{said!r} would have lost its tools"

    @pytest.mark.parametrize("said", [
        "play my playlist Polonia from Apple Music",
        "what is in my calendar today",
        "show me that on screen",
        "type this into the chat box",
    ])
    def test_none_of_it_reaches_agent_mode(self, said):
        # Agent mode discarded the tool that could have done the work and
        # answered "playing Polonia from Apple Music now" with the app closed.
        assert owned(said), f"{said!r} is unclaimed, so agent mode can take it"


@pytest.mark.unit
class TestActingVerbsNeverLeaveTheMachine:
    """open/launch/start/play act on his machine, which agent mode cannot touch.

    Each of those verbs was lost to agent mode in turn and patched with a
    narrower hint, until "open screwfix hotdogzone 2" slipped between the
    hints and agent mode answered by launching Canva. The fix is categorical:
    verbs that mean "do something here" do not trigger the mode that swaps
    away the tools which can do it.
    """

    @pytest.mark.parametrize("said", [
        "open screwfix hotdogzone 2",
        "open the garage folder",
        "launch the usage monitor",
        "start my morning routine",
        "play something calm",
    ])
    def test_no_acting_verb_reaches_agent_mode(self, said):
        assert not bridge.AGENT_HINT.search(said),             f"{said!r} would be routed away from the machine's owner"

    @pytest.mark.parametrize("said", [
        "search for hot dog stand suppliers",
        "look up the latest canva pricing",
        "what is the latest news on the euro",
    ])
    def test_going_out_to_the_world_still_does(self, said):
        assert bridge.AGENT_HINT.search(said)


@pytest.mark.unit
class TestItDoesNotOverreach:
    @pytest.mark.parametrize("said", [
        "what is the capital of Poland",
        "how are you today",
        "make me a poster for the shop",
        "write a script that renames these files",
    ])
    def test_ordinary_requests_are_left_alone(self, said):
        assert owned(said) == set(), f"{said!r} was claimed and should not be"

    def test_a_tool_that_was_never_attached_claims_nothing(self):
        # The daemon's router decides what this request is; a flag that fired
        # without the tool would route work to a lane that cannot do it.
        assert owned("play my playlist Polonia", attached("localFiles")) == set()

    def test_the_music_flag_needs_a_music_word(self):
        assert "music" not in owned("play along with whatever he says")


@pytest.mark.unit
class TestPicturesStayWithTheDaemon:
    """Finding a photograph is the daemon's job, whatever the verb.

    "Search every picture on my machine for a white Lamborghini" matched
    the agent hint on the word "search", agent mode threw the daemon's tool
    list away, and Claude Code walked the disk by filename — the one search
    that cannot work, since the picture had been renamed on purpose.
    """

    IMAGE_TOOLS = [{"function": {"name": "findImage"}},
                   {"function": {"name": "viewImage"}}]

    @pytest.mark.parametrize("said", [
        "search every picture on the whole machine for a white lamborghini",
        "find the photo of a white lamborghini on my pc",
        "look for that image of the hot dog van",
        "what's in the screenshot I just sent",
    ])
    def test_picture_work_is_kept(self, said):
        assert bridge.daemon_owns(said, self.IMAGE_TOOLS)["images"]

    @pytest.mark.parametrize("said", [
        "search the web for lamborghini prices",
        "what's the latest news",
    ])
    def test_ordinary_searching_still_goes_out(self, said):
        assert not bridge.daemon_owns(said, self.IMAGE_TOOLS)["images"]

    def test_it_needs_the_tool_to_be_offered(self):
        assert not bridge.daemon_owns("find the photo of the car", [])["images"]


@pytest.mark.unit
class TestAMalformedToolCallIsNotSpoken:
    """A page of JSON was read out as the answer.

    The reply opened with the tool marker, the parser could not make sense
    of it, and the fallback emitted the raw text as content. Whatever else
    happens, the emulated protocol must not reach him as speech.
    """

    def test_a_good_marker_still_parses(self):
        call = bridge._parse_tool_call(
            'TOOL_CALL:{"name": "localFiles", "arguments": {"operation": "read"}}')
        assert call and call["function"]["name"] == "localFiles"

    @pytest.mark.parametrize("text", [
        'TOOL_CALL:{"name": "driveApp", "arguments": {"steps": [{"action":',
        'TOOL_CALL:{not json at all}',
    ])
    def test_a_broken_marker_does_not_parse(self, text):
        # Which is what sends it down the path that used to speak it.
        assert bridge._parse_tool_call(text) is None

    def test_the_opening_is_still_recognised_as_a_tool_call(self):
        assert bridge._tool_call_verdict('TOOL_CALL:{"name": "driveApp"') is True
