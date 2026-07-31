#!/usr/bin/env python
"""Report whether the agent's API key is configured. Never prints the key."""

import os


def main() -> None:
    """Print whether ANTHROPIC_API_KEY is visible to project commands."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        print(f"ANTHROPIC_API_KEY is set (ends with ...{key[-4:]})")
    else:
        print("ANTHROPIC_API_KEY is NOT set.")
        print("  - project-local: copy .env.example to .env and fill it in")
        print("  - machine-wide (Windows): setx ANTHROPIC_API_KEY <your-key>")
        print("  - or authenticate once with: ant auth login")
    model = os.environ.get("HDH_AGENT_MODEL")
    if model:
        print(f"HDH_AGENT_MODEL override: {model}")


if __name__ == "__main__":
    main()
