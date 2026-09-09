"""Deterministic tests for AI agents."""
from .session import record, replay, assert_replays, Divergence
from . import store, chain, diff, evaluate, model

try:                                    # installed
    from importlib.metadata import version, PackageNotFoundError
    try:
        __version__ = version("Orientim")
    except PackageNotFoundError:        # running from a checkout
        __version__ = "0.1.0+source"
except ImportError:                     # 3.7 fallback, harmless below the floor
    __version__ = "0.1.0+source"

__all__ = ["record", "replay", "assert_replays", "Divergence",
           "store", "chain", "diff", "evaluate", "model",
           "__version__"]
