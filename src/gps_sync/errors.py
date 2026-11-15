"""Failure types that map to process exit code 2."""


class ToolFailure(Exception):
    """A tool-level failure that aborts the run before any per-file outcome exists."""


class InternalConsistencyError(ToolFailure):
    """A guarantee an upstream stage owes the writer was violated; nothing was mutated."""


class TrackIdentityCollision(ToolFailure):
    """Two track segments were assigned the same identity, which would defeat the segment guard."""
