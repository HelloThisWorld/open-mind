"""Semantic provider adapters, profiles and the registry (v2 Phase 4).

Import discipline: importing THIS package must never import a provider SDK.
Every adapter module imports its SDK lazily inside the methods that need it,
so a missing ``openai`` or ``anthropic`` package degrades exactly one provider
kind and nothing else.
"""
