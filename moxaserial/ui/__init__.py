"""Fusion-only UI layer.

Everything in this package may import ``adsk`` - but only *lazily*, inside
functions, so that importing ``moxaserial.ui.lastpost`` (for example) from a test
on a machine without Fusion still works and simply reports "no Fusion".
"""
