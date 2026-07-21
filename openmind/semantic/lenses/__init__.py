"""Adaptive Project Lenses (v2 Phase 4).

A lens is a small, CLOSED, declarative description of how one project is
organized — which roles files play, which identifier schemes its documents
use, which semantic tasks are worth running where. Three sources, one
schema:

* ``builtin``      — read-only projections of the existing Template Profiles
  (the templates themselves are untouched and keep driving detection,
  facets, guides and the ``.openmind`` export exactly as before);
* ``organization`` — user-managed lens files from a local directory;
* ``induced``      — proposed by a strong-tier model from bounded
  representative samples, stored ``provisional``, deterministically
  validated, and requiring explicit human approval AND explicit activation.

An active lens influences SEMANTIC PLANNING ONLY. It never contains
executable content, never rewrites deterministic ingestion, and an induced
one can never activate itself.
"""
