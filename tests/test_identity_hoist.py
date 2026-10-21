"""The identity belongs in the cached part of the prompt.

Measured on this machine, per cold worker start, same method both sides
(``cache_creation_input_tokens`` / ``cache_read_input_tokens`` off the CLI's
own result event):

    identity in the user message      created ~8,905   read ~16,780
    identity in an appended system
      prompt file, dynamic sections
      excluded                        created ~5,053   read  20,338

The identity moved out of the half that is paid for on every cold start and
into the half that is read back. What the tests below pin is the part that can
silently regress: which modes wear the persona, that the text is only removed
from the message when the worker really is carrying it, and that a missing
identity file leaves everything exactly as it was.
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


@pytest.fixture
def identity(tmp_path, monkeypatch):
    """An identity file, in the shape the daemon writes it."""
    path = tmp_path / "identity.md"
    path.write_text(
        "<!-- This is how Jarvis talks. Edit it and the change applies to the\n"
        "     very next reply. -->\n\n"
        "Persona: you are a British butler named Jarvis.\n",
        encoding="utf-8")
    monkeypatch.setattr(bridge, "IDENTITY_PATH", path)
    monkeypatch.setattr(bridge, "STABLE_DIR", tmp_path)
    monkeypatch.setattr(bridge, "_STABLE", {"text": None, "mtime": None})
    return path


@pytest.mark.unit
class TestWhatCountsAsStable:
    def test_the_note_to_the_human_is_not_sent_to_the_model(self, identity):
        text = bridge.stable_identity()
        assert "British butler" in text
        assert "Edit it" not in text

    def test_an_edit_is_picked_up(self, identity):
        assert "butler" in bridge.stable_identity()
        identity.write_text("Persona: you are a lighthouse keeper.", encoding="utf-8")
        # mtime resolution is coarse enough that a same-second rewrite can look
        # unchanged; the size differs, which is what the cache also keys on.
        assert "lighthouse" in bridge.stable_identity()

    def test_no_identity_file_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "IDENTITY_PATH", tmp_path / "gone.md")
        monkeypatch.setattr(bridge, "_STABLE", {"text": None, "mtime": None})
        assert bridge.stable_identity() == ""


@pytest.mark.unit
class TestWhichModesWearThePersona:
    def test_a_spoken_lane_carries_it_in_the_system_prompt(self, identity):
        cmd = bridge._worker_cmd("chat")
        assert "--append-system-prompt-file" in cmd
        written = Path(cmd[cmd.index("--append-system-prompt-file") + 1])
        assert "British butler" in written.read_text(encoding="utf-8")

    @pytest.mark.parametrize("mode", ["agent", "canva"])
    def test_the_other_spoken_lanes_carry_it_too(self, identity, mode):
        assert "--append-system-prompt-file" in bridge._worker_cmd(mode)

    @pytest.mark.parametrize("mode", ["fast", "bg"])
    def test_the_machine_lanes_do_not(self, identity, mode):
        # These answer the daemon's own classification and summarisation calls,
        # whose output has to stay parseable. A butler's voice in a tool-router
        # prompt is a routing bug waiting to happen.
        assert "--append-system-prompt-file" not in bridge._worker_cmd(mode)

    def test_nothing_is_appended_when_there_is_no_identity_file(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(bridge, "IDENTITY_PATH", tmp_path / "gone.md")
        monkeypatch.setattr(bridge, "_STABLE", {"text": None, "mtime": None})
        assert "--append-system-prompt-file" not in bridge._worker_cmd("chat")

    def test_the_per_machine_sections_move_out_of_the_cached_prefix(self, identity):
        # This flag is what makes the prefix identical between two cold
        # processes; without it the cwd and git status differ and nothing after
        # them can be read back.
        for mode in ("chat", "fast", "agent", "canva", "bg"):
            assert "--exclude-dynamic-system-prompt-sections" in bridge._worker_cmd(mode)


@pytest.mark.unit
class TestTheMessageDoesNotRepeatIt:
    def _worker(self, text):
        worker = bridge.ClaudeWorker("chat")
        worker.stable_text = text
        return worker

    def test_the_worker_removes_its_own_copy(self):
        persona = "Persona: you are a British butler named Jarvis."
        worker = self._worker(persona)
        sent = worker._without_stable(
            f"<instructions>\n{persona}\n\nCurrent local time: noon\n"
            f"</instructions>\n\nUser: hello")
        assert persona not in sent
        assert "Current local time: noon" in sent
        assert "User: hello" in sent

    def test_a_prompt_from_before_the_edit_is_left_whole(self):
        # The worker holds yesterday's persona and the message carries today's.
        # Sending it anyway costs tokens; dropping it would cost the personality.
        worker = self._worker("Persona: you are a British butler.")
        prompt = "<instructions>\nPersona: you are a lighthouse keeper.\n</instructions>\n\nUser: hi"
        assert worker._without_stable(prompt) == prompt

    def test_a_worker_with_no_persona_changes_nothing(self):
        worker = self._worker("")
        prompt = "<instructions>\nanything\n</instructions>\n\nUser: hi"
        assert worker._without_stable(prompt) == prompt
