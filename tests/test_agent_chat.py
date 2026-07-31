"""Offline tests for the chat session's context compaction.

These run without an Anthropic API key: the summarizer is injected, so they
double as the demonstration that a conversation beyond 100 messages is
summarized down while recent turns survive verbatim.
"""

import pytest

pytest.importorskip("anthropic")

from hdh.modules.agent.chat import (
    ChatSession, find_clean_cut, is_clean_user_message, render_transcript,
)


def fake_conversation(n_turns: int) -> list:
    """A synthetic conversation: every 3rd exchange includes a tool round-trip."""
    messages = []
    for i in range(n_turns):
        messages.append({"role": "user", "content": f"Question {i} about patient MRN0000000{i % 10}?"})
        if i % 3 == 0:
            messages.append({"role": "assistant", "content": [
                {"type": "tool_use", "id": f"tu_{i}", "name": "get_patient_chart",
                 "input": {"mrn": f"MRN0000000{i % 10}"}},
            ]})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"tu_{i}",
                 "content": "CHART DATA " * 100},
            ]})
        messages.append({"role": "assistant", "content": [
            {"type": "text", "text": f"Answer {i}: the patient is stable."},
        ]})
    return messages


def test_clean_user_detection():
    assert is_clean_user_message({"role": "user", "content": "hi"})
    assert not is_clean_user_message({"role": "assistant", "content": "hi"})
    assert not is_clean_user_message({"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "content": "y"}]})


def test_clean_cut_never_orphans_tool_results():
    messages = fake_conversation(50)
    for keep in (5, 10, 20, 33):
        cut = find_clean_cut(messages, keep)
        assert is_clean_user_message(messages[cut])


def test_compaction_beyond_100_messages():
    """The headline demo: 100+ message history collapses to summary + recent."""
    chat = ChatSession(db_session=None, max_messages=100, keep_recent=20,
                       summarizer=lambda t: "Summary of the earlier conversation.")
    chat.messages = fake_conversation(45)   # ~150 messages
    n_before = len(chat.messages)
    assert n_before > 100

    event = chat.maybe_compact()
    assert event is not None
    assert event.messages_before == n_before
    assert event.messages_after == len(chat.messages) < n_before

    # First message is the summary block, and it's a valid opening user turn
    first = chat.messages[0]
    assert first["role"] == "user"
    assert first["content"].startswith("<conversation_summary>")
    # The message right after the summary is a clean user turn (no orphaned tool_result)
    assert is_clean_user_message(chat.messages[1])
    # Recent turns survive verbatim
    assert chat.messages[-1]["content"][0]["text"].startswith("Answer 44")

    def fake_summarizer(transcript):
        # The summarizer sees the old turns' content, including MRNs
        assert "MRN" in transcript
        return "Discussed patients MRN00000001-9; all stable; follow-ups pending."

    # compact() is idempotent-ish: a second manual pass with few messages left
    chat2 = ChatSession(db_session=None, max_messages=100, keep_recent=20)
    chat2.messages = fake_conversation(45)
    chat2.compact(summarizer=fake_summarizer)
    assert "MRN00000001-9" in chat2.messages[0]["content"]


def test_below_threshold_no_compaction():
    chat = ChatSession(db_session=None, max_messages=100, keep_recent=20)
    chat.messages = fake_conversation(10)
    assert chat.maybe_compact() is None
    assert len(chat.messages) == len(fake_conversation(10))


def test_transcript_truncates_tool_results():
    messages = fake_conversation(4)
    transcript = render_transcript(messages, max_result_chars=50)
    assert "chars total]" in transcript
    assert "Question 0" in transcript


def test_display_and_markdown_export():
    chat = ChatSession(db_session=None, keep_recent=5)
    chat.messages = fake_conversation(10)
    event = chat.compact(summarizer=lambda t: "Earlier: nothing notable.")
    assert event is not None
    kinds = [k for k, _ in chat.display_events()]
    assert "summary" in kinds
    md = chat.to_markdown()
    assert "**You:**" in md and "**Agent:**" in md
    assert "Earlier: nothing notable." in md
    assert set(kinds) <= {"summary", "user", "assistant", "tool"}
