"""
Multi-turn chat session with automatic context compaction.

The full conversation — user turns, assistant turns, tool calls, and tool
results — is kept as API-format messages so the agent has real memory across
questions. When the history grows past ``max_messages`` (default 100), the
older portion is summarized by the model into a compact ``<conversation_summary>``
block that replaces it, keeping recent turns verbatim. This bounds context
growth in long conversations while preserving the clinically important facts
(MRNs, findings, decisions).
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from .agent import DEFAULT_MODEL, SYSTEM_PROMPT

SUMMARIZER_PROMPT = """\
You summarize the earlier part of a care-team conversation with an AI assistant
over a synthetic family-medicine dataset. Produce a compact briefing that lets
the assistant continue seamlessly. Preserve exactly: patient MRNs and names
discussed, clinical findings, care gaps, risk scores, SQL results that were
acted on, decisions made, and open follow-ups. Use terse bullet points grouped
by topic. Omit pleasantries and tool mechanics. Maximum ~400 words.\
"""


def _block_type(block) -> str:
    return (block.get("type") if isinstance(block, dict) else getattr(block, "type", "")) or ""


def _block_text(block) -> str:
    return (block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")) or ""


def is_clean_user_message(message) -> bool:
    """A user message that is plain text (safe compaction cut point).

    Cutting on a tool_result message would orphan it from its assistant
    tool_use turn, which the API rejects.
    """
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return all(_block_type(b) != "tool_result" for b in content)


def find_clean_cut(messages: list, keep_recent: int) -> int:
    """Earliest index >= len-keep_recent that starts a clean user turn."""
    cut = max(len(messages) - keep_recent, 1)
    while cut < len(messages) and not is_clean_user_message(messages[cut]):
        cut += 1
    return cut


def render_transcript(messages: list, max_result_chars: int = 400) -> str:
    """Render API messages as a plain-text transcript (for the summarizer)."""
    lines = []
    for m in messages:
        role = str((m.get("role") if isinstance(m, dict) else m["role"]) or "")
        content = (m.get("content") if isinstance(m, dict) else m["content"]) or ""
        if isinstance(content, str):
            lines.append(f"{role.upper()}: {content}")
            continue
        for b in content:
            btype = _block_type(b)
            if btype == "text":
                lines.append(f"{role.upper()}: {_block_text(b)}")
            elif btype == "tool_use":
                name = b.get("name") if isinstance(b, dict) else b.name
                args = b.get("input") if isinstance(b, dict) else b.input
                lines.append(f"ASSISTANT called tool {name}({args})")
            elif btype == "tool_result":
                raw = b.get("content") if isinstance(b, dict) else getattr(b, "content", "")
                text = raw if isinstance(raw, str) else str(raw)
                if len(text) > max_result_chars:
                    text = text[:max_result_chars] + f"... [{len(text)} chars total]"
                lines.append(f"TOOL RESULT: {text}")
    return "\n".join(lines)


@dataclass
class CompactionEvent:
    at: str
    messages_before: int
    messages_after: int
    summary: str


@dataclass
class ChatSession:
    """A persistent multi-turn conversation with the care-program agent."""

    db_session: object
    model: str | None = None
    max_messages: int = 100  # auto-compact once history exceeds this
    keep_recent: int = 20  # messages kept verbatim after compaction
    summarizer: Callable[[str], str] | None = None  # override (default: the model)
    messages: list = field(default_factory=list)
    compactions: list = field(default_factory=list)

    def __post_init__(self):
        self.model = self.model or os.environ.get("HDH_AGENT_MODEL", DEFAULT_MODEL)
        self._client = None
        self._tools = None

    @property
    def client(self):
        if self._client is None:
            import anthropic

            # Lazy default factory; tests/callers may inject via _client.
            self._client = anthropic.Anthropic()  # quality: allow(dependency-injection)
        return self._client

    @property
    def tools(self):
        if self._tools is None:
            from .tools import build_tools

            self._tools = build_tools(self.db_session)
        return self._tools

    # ── Conversation ─────────────────────────────────────────────────────────

    def ask(self, question: str, on_tool=None) -> tuple[str, "CompactionEvent | None"]:
        """Ask a question in this conversation; returns the final answer text.

        ``on_tool(tool_use_block)`` is called for each tool invocation so the
        UI can show a live trace.
        """
        compacted = self.maybe_compact()
        self.messages.append({"role": "user", "content": question})

        params = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=list(self.messages),
        )
        # Server-side refusal fallbacks exist only on the Opus 5 / Fable 5
        # family; other models (e.g. claude-sonnet-4-6) reject the parameter.
        if (self.model or "").startswith(("claude-opus-5", "claude-fable", "claude-mythos")):
            try:
                runner = self.client.beta.messages.tool_runner(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **params,
                )
            except TypeError:
                # Older SDK without the fallbacks parameter — run without it.
                runner = self.client.beta.messages.tool_runner(**params)
        else:
            runner = self.client.beta.messages.tool_runner(**params)

        final_text = ""
        for message in runner:
            # Mirror the runner's history so the conversation persists
            self.messages.append({"role": "assistant", "content": message.content})
            if message.stop_reason == "refusal":
                final_text = "The request was declined by the model's safety system."
                break
            for b in message.content:
                if _block_type(b) == "tool_use" and on_tool:
                    on_tool(b)
            texts = [_block_text(b) for b in message.content if _block_type(b) == "text"]
            if texts:
                final_text = "\n".join(t for t in texts if t)
            tool_response = runner.generate_tool_call_response()
            if tool_response is not None:
                self.messages.append(tool_response)
        return final_text, compacted

    # ── Context management ───────────────────────────────────────────────────

    def maybe_compact(self):
        """Auto-compact when the history exceeds max_messages."""
        if len(self.messages) > self.max_messages:
            return self.compact()
        return None

    def compact(self, summarizer: Callable[[str], str] | None = None) -> "CompactionEvent | None":
        """Summarize everything but the most recent turns into one block.

        ``summarizer(transcript) -> str`` can be injected (tests, offline
        demos); the default asks the model itself.
        """
        cut = find_clean_cut(self.messages, self.keep_recent)
        if cut < 2 or cut >= len(self.messages):
            return None  # nothing worth compacting / no clean cut available

        old, recent = self.messages[:cut], self.messages[cut:]
        transcript = render_transcript(old)
        summary = (summarizer or self.summarizer or self._llm_summarize)(transcript)

        summary_message = {
            "role": "user",
            "content": (
                f"<conversation_summary>\n{summary}\n</conversation_summary>\n"
                f"(The earlier {len(old)} messages of this conversation were "
                f"summarized above. Continue the conversation seamlessly.)"
            ),
        }
        event = CompactionEvent(
            at=datetime.now().strftime("%H:%M:%S"),
            messages_before=len(self.messages),
            messages_after=1 + len(recent),
            summary=summary,
        )
        self.messages = [summary_message] + recent
        self.compactions.append(event)
        return event

    def _llm_summarize(self, transcript: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SUMMARIZER_PROMPT,
            messages=[{"role": "user", "content": transcript}],
        )
        return next(
            (_block_text(b) for b in response.content if _block_type(b) == "text"), "(summary unavailable)"
        )

    # ── Introspection for the UI ─────────────────────────────────────────────

    def token_count(self) -> int:
        """Actual input-token count of the current context (API-measured);
        falls back to a chars/4 estimate if counting fails."""
        try:
            resp = self.client.messages.count_tokens(
                model=self.model, system=SYSTEM_PROMPT, messages=self.messages
            )
            return resp.input_tokens
        except Exception:
            return len(render_transcript(self.messages, max_result_chars=10**9)) // 4

    def display_events(self):
        """Yield (kind, text) pairs for rendering the chat history.

        Kinds: 'summary', 'user', 'assistant', 'tool'.
        """
        for m in self.messages:
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, str):
                if content.startswith("<conversation_summary>"):
                    yield "summary", content
                else:
                    yield role, content
                continue
            for b in content:
                btype = _block_type(b)
                if btype == "text" and _block_text(b):
                    yield role, _block_text(b)
                elif btype == "tool_use":
                    name = b.get("name") if isinstance(b, dict) else b.name
                    args = (b.get("input") if isinstance(b, dict) else b.input) or {}
                    yield "tool", f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})"

    def to_markdown(self) -> str:
        """Export the visible conversation as a Markdown transcript."""
        lines = [f"# hdh agent chat — {datetime.now():%Y-%m-%d %H:%M}", ""]
        for kind, text in self.display_events():
            if kind == "user":
                lines += [f"**You:** {text}", ""]
            elif kind == "assistant":
                lines += [f"**Agent:** {text}", ""]
            elif kind == "tool":
                lines += [f"> 🔧 `{text}`", ""]
            elif kind == "summary":
                lines += ["---", "*Earlier conversation (summarized):*", "", text, "---", ""]
        return "\n".join(lines)
