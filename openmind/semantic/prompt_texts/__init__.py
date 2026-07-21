"""Versioned prompt texts.

One module per prompt family and version (``…_v1``). A released module is
IMMUTABLE: its rendered text is hashed into every run record and every cache
key, so changing behavior means adding ``…_v2`` and bumping the task's
``prompt_version`` — never editing a released file.
"""
