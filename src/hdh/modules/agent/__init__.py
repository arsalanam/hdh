"""Agentic AI care assistant.

A Claude-powered agent (Anthropic SDK tool-use loop) with tools over the
synthetic dataset: patient lookup, cohort search, care gaps, risk scores,
and read-only SQL. Requires the ``agent`` extra (``pip install hdh[agent]``)
and an Anthropic API key (``ANTHROPIC_API_KEY`` or ``ant auth login``).
"""
