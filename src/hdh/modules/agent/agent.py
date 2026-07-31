"""
The care-program agent loop.

Uses the Anthropic SDK's tool runner: Claude decides which database tools to
call, the runner executes them and feeds results back, and iteration ends when
Claude has its answer. Server-side refusal fallbacks are enabled by default so
a safety-classifier decline is retried on Anthropic's recommended fallback
model instead of failing the request.
"""

import os

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


def run_agent(session, question: str, model: str = None, verbose: bool = True) -> str:
    """Ask the agent one question; returns the final answer text."""
    import anthropic
    from .tools import build_tools

    client = anthropic.Anthropic()
    tools = build_tools(session)
    model = model or os.environ.get("HDH_AGENT_MODEL", DEFAULT_MODEL)

    params = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=tools,
        messages=[{"role": "user", "content": question}],
    )
    try:
        runner = client.beta.messages.tool_runner(
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            **params,
        )
    except TypeError:
        # Older SDK without the fallbacks parameter — run without it.
        runner = client.beta.messages.tool_runner(**params)

    final_text = ""
    for message in runner:
        if message.stop_reason == "refusal":
            return "The request was declined by the model's safety system."
        texts = [b.text for b in message.content if b.type == "text"]
        tool_calls = [b for b in message.content if b.type == "tool_use"]
        if verbose:
            for tc in tool_calls:
                print(f"  🔧 {tc.name}({', '.join(f'{k}={v!r}' for k, v in tc.input.items())})")
        if texts:
            final_text = "\n".join(texts)
    return final_text
