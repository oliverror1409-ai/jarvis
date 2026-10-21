"""Keeping Claude Code alive on NVIDIA when the subscription window empties.

The client speaks one dialect and the fallback models speak another, so the
whole thing rests on translating between them faithfully. Two properties
matter more than the rest: a tool call has to survive the round trip, because
tool calls are what edit his files; and a genuine error has to stay an error,
because answering a bug with a weaker model hides the bug and wastes the
session that was meant to be rescued.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proxy", ROOT / "claude-code-nim.py")
proxy = importlib.util.module_from_spec(spec)
sys.modules["proxy"] = proxy
spec.loader.exec_module(proxy)


@pytest.mark.unit
class TestSendingTheConversationOn:
    def test_the_system_prompt_leads(self):
        out = proxy.to_openai_messages({"system": "You are terse.",
                                        "messages": [{"role": "user", "content": "hi"}]})
        assert out[0] == {"role": "system", "content": "You are terse."}

    def test_a_tool_call_survives_the_translation(self):
        # This is what edits his files. If it does not survive, the session
        # looks alive and changes nothing.
        out = proxy.to_openai_messages({"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "Read",
                 "input": {"file_path": "README.md"}}]},
        ]})
        call = out[0]["tool_calls"][0]
        assert call["function"]["name"] == "Read"
        assert json.loads(call["function"]["arguments"]) == {"file_path": "README.md"}

    def test_a_tool_result_becomes_its_own_turn(self):
        out = proxy.to_openai_messages({"messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": "file contents"}]}]},
        ]})
        assert out[0]["role"] == "tool"
        assert out[0]["tool_call_id"] == "toolu_1"
        assert "file contents" in out[0]["content"]

    def test_an_empty_tool_result_still_says_something(self):
        # An empty content field is rejected by some models outright.
        out = proxy.to_openai_messages({"messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": []}]},
        ]})
        assert out[0]["content"].strip()

    def test_tools_carry_their_schemas(self):
        tools = proxy.to_openai_tools({"tools": [
            {"name": "Edit", "description": "Edit a file",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
        ]})
        assert tools[0]["function"]["name"] == "Edit"
        assert tools[0]["function"]["parameters"]["properties"]["path"]


@pytest.mark.unit
class TestHandingTheAnswerBack:
    def test_plain_text_comes_back_as_a_text_block(self):
        message = proxy.to_anthropic_message(
            {"message": {"content": "done"}}, "some-model", {})
        assert message["content"] == [{"type": "text", "text": "done"}]
        assert message["stop_reason"] == "end_turn"

    def test_a_tool_call_comes_back_as_tool_use(self):
        message = proxy.to_anthropic_message({"message": {"tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "Read", "arguments": '{"file_path": "a.txt"}'}}]}},
            "some-model", {})
        block = message["content"][0]
        assert block["type"] == "tool_use" and block["name"] == "Read"
        assert block["input"] == {"file_path": "a.txt"}
        # The client keeps generating until it sees this, so it has to be right.
        assert message["stop_reason"] == "tool_use"

    def test_unparseable_arguments_do_not_crash_the_turn(self):
        message = proxy.to_anthropic_message({"message": {"tool_calls": [
            {"id": "c", "function": {"name": "Read", "arguments": "{not json"}}]}},
            "m", {})
        assert message["content"][0]["input"]["_unparsed"]

    def test_a_silent_model_still_produces_a_valid_message(self):
        message = proxy.to_anthropic_message({"message": {"content": ""}}, "m", {})
        assert message["content"], "a message with no blocks is rejected by the client"

    def test_the_event_stream_is_in_the_expected_order(self):
        message = proxy.to_anthropic_message({"message": {"content": "hello"}}, "m", {})
        events = [line.split(": ", 1)[1]
                  for line in b"".join(proxy.as_sse(message)).decode().splitlines()
                  if line.startswith("event: ")]
        assert events == ["message_start", "content_block_start",
                          "content_block_delta", "content_block_stop",
                          "message_delta", "message_stop"]


@pytest.mark.unit
class TestWhenToFallBackAtAll:
    @pytest.mark.parametrize("status,body", [
        (429, b'{"error":{"message":"rate limit exceeded"}}'),
        (400, b'{"error":{"message":"usage limit reached for this window"}}'),
        (403, b'{"error":{"message":"credit balance too low"}}'),
    ])
    def test_a_spent_window_falls_back(self, status, body):
        assert proxy.spent_subscription(status, body)

    @pytest.mark.parametrize("status,body", [
        (401, b'{"error":{"message":"invalid x-api-key"}}'),
        (404, b'{"error":{"message":"model not found"}}'),
        (400, b'{"error":{"message":"max_tokens must be greater than 0"}}'),
        (500, b'{"error":{"message":"internal server error"}}'),
    ])
    def test_a_real_error_is_handed_back_untouched(self, status, body):
        # Answering a bad request with a weaker model hides the bad request
        # and burns the session it was meant to save.
        assert not proxy.spent_subscription(status, body)
