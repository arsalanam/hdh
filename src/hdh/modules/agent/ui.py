"""
Interactive chat UI for the care-program agent.

Rich terminal rendering (markdown answers, chat history, compaction notices)
with prompt_toolkit input: arrow-key recall of previous questions, persisted
across sessions in ~/.hdh/agent_prompt_history.

Slash commands:
  /history   show the full conversation so far
  /context   context size: messages, tokens, compactions
  /compact   summarize older turns now (auto-triggers past --compact-after)
  /save [f]  export the transcript as Markdown
  /clear     start a fresh conversation
  /help      command help
  /exit      leave
"""

from pathlib import Path

HELP = """\
| Command | Effect |
|---|---|
| `/history` | Show the full chat history |
| `/context` | Show context size (messages, tokens) and compaction log |
| `/compact` | Summarize older turns into a compact briefing now |
| `/save [file]` | Export transcript as Markdown |
| `/clear` | Start a fresh conversation |
| `/exit` | Quit (Ctrl-D also works) |
"""


def run_ui(chat, compact_after: int):
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel

    console = Console()
    try:
        # Arrow-key input history, persisted across sessions. Falls back to
        # plain input() where prompt_toolkit can't drive the terminal
        # (e.g. Git Bash / mintty on Windows, piped stdin).
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory

        history_dir = Path.home() / ".hdh"
        history_dir.mkdir(exist_ok=True)
        _prompt: PromptSession = PromptSession(history=FileHistory(str(history_dir / "agent_prompt_history")))

        def read_line():
            return _prompt.prompt("you> ")
    except Exception:

        def read_line():
            return input("you> ")

    console.print(
        Panel(
            f"[bold]hdh care-program agent[/bold] — model [cyan]{chat.model}[/cyan]\n"
            f"Conversation is remembered across questions; beyond "
            f"[cyan]{compact_after}[/cyan] messages, older turns are auto-summarized.\n"
            f"Type [cyan]/help[/cyan] for commands, [cyan]/exit[/cyan] to quit.",
            title="🩺 interactive chat",
            border_style="cyan",
        )
    )

    def show_compaction(event):
        console.print(
            Panel(
                f"Context compacted at {event.at}: "
                f"[bold]{event.messages_before} → {event.messages_after}[/bold] messages.\n"
                f"Older turns replaced by this summary:\n\n{event.summary}",
                title="🗜  context compaction",
                border_style="yellow",
            )
        )

    def show_history():
        if not chat.messages:
            console.print("[dim]No conversation yet.[/dim]")
            return
        for kind, text in chat.display_events():
            if kind == "user":
                console.print(Panel(text, title="you", title_align="left", border_style="green"))
            elif kind == "assistant":
                console.print(Panel(Markdown(text), title="agent", title_align="left", border_style="blue"))
            elif kind == "tool":
                console.print(f"  [dim]🔧 {text}[/dim]")
            elif kind == "summary":
                console.print(Panel(text, title="earlier conversation (summarized)", border_style="yellow"))

    def show_context():
        with console.status("[dim]counting tokens...[/dim]"):
            tokens = chat.token_count()
        console.print(
            f"  messages in context : [bold]{len(chat.messages)}[/bold] "
            f"(auto-compacts beyond {chat.max_messages})\n"
            f"  input tokens        : [bold]{tokens:,}[/bold]\n"
            f"  compactions so far  : [bold]{len(chat.compactions)}[/bold]"
        )
        for e in chat.compactions:
            console.print(f"    [dim]{e.at}  {e.messages_before} → {e.messages_after} messages[/dim]")

    def handle_command(line: str) -> bool:
        """Returns False when the UI should exit."""
        cmd, _, arg = line.partition(" ")
        if cmd in ("/exit", "/quit"):
            return False
        elif cmd == "/help":
            console.print(Markdown(HELP))
        elif cmd == "/history":
            show_history()
        elif cmd == "/context":
            show_context()
        elif cmd == "/compact":
            with console.status("[yellow]summarizing older turns...[/yellow]"):
                event = chat.compact()
            if event:
                show_compaction(event)
            else:
                console.print("[dim]Nothing to compact yet — need a longer conversation.[/dim]")
        elif cmd == "/save":
            path = Path(arg.strip() or "chat_transcript.md")
            path.write_text(chat.to_markdown(), encoding="utf-8")
            console.print(f"[green]Transcript saved → {path}[/green]")
        elif cmd == "/clear":
            chat.messages.clear()
            chat.compactions.clear()
            console.clear()
            console.print("[dim]Conversation cleared.[/dim]")
        else:
            console.print(f"[red]Unknown command {cmd}[/red] — try /help")
        return True

    while True:
        try:
            line = read_line().strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.startswith("/"):
            if not handle_command(line):
                break
            continue

        def on_tool(block):
            args = ", ".join(f"{k}={v!r}" for k, v in block.input.items())
            console.print(f"  [dim]🔧 {block.name}({args})[/dim]")

        try:
            with console.status("[cyan]thinking...[/cyan]"):
                answer, compacted = chat.ask(line, on_tool=on_tool)
        except Exception as e:
            console.print(f"[red]Error:[/red] {e}")
            continue
        if compacted:
            show_compaction(compacted)
        console.print(
            Panel(Markdown(answer or "(no answer)"), title="agent", title_align="left", border_style="blue")
        )

    console.print("[dim]bye 👋[/dim]")
