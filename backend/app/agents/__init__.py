"""Agent pipeline contracts, runtime and evidence.

This package holds the multi-agent pipeline that recommendation.html and
recommendations_validation.html specify. It is deliberately additive: nothing
here bypasses the existing hash-bound approval boundary, and nothing here
writes the music state except through ``MusicService.save_draft``.

The records module is the ledger the retro agent will later read. It writes a
separate file from ``attempts.jsonl`` on purpose: that journal's parser treats
an unknown row kind as corruption, so new evidence must not share it.
"""
from __future__ import annotations

__all__ = ["records"]
