"""Deriving and comparing what a run means, once it has been executed.

Kept apart from ``models`` (which defines the shapes) and from ``runner`` (which
produces the evidence). Both of those import this, and nothing here imports
either transport or containment: a result's meaning is decided from the recorded
evidence, never by asking the target another question.
"""
