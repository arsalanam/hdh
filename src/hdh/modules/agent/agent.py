"""
The care-program agent loop.

Uses the Anthropic SDK's tool runner: Claude decides which database tools to
call, the runner executes them and feeds results back, and iteration ends when
Claude has its answer. Server-side refusal fallbacks are enabled by default so
a safety-classifier decline is retried on Anthropic's recommended fallback
model instead of failing the request.
"""

DEFAULT_MODEL = "claude-opus-5"

SYSTEM_PROMPT = """\
You are a clinical care-program assistant for a family medicine practice,
working over a fully SYNTHETIC dataset (no real patients, no PHI).

You have tools to look up patient charts, search cohorts, list care gaps,
read ML risk scores, run read-only SQL, and get dataset statistics. Use them
to ground every answer in the actual data — do not guess values you could
look up. When the answer depends on patient data, call a tool first.

Answer like a colleague: lead with the finding, keep it concise, and cite
MRNs so the care team can act on your answer. This is synthetic data for
software development — clinical realism matters, but no medical advice is
being given to real people.
"""


def run_agent(session, question: str, model: str | None = None, verbose: bool = True) -> str:
    """Ask the agent one question (fresh conversation); returns the answer text."""
    from .chat import ChatSession

    chat = ChatSession(db_session=session, model=model)

    def on_tool(block):
        print(f"  🔧 {block.name}({', '.join(f'{k}={v!r}' for k, v in block.input.items())})")

    answer, _ = chat.ask(question, on_tool=on_tool if verbose else None)
    return answer
