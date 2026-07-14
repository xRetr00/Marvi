"""Local full-text Brain index."""

from .indexer import index_configured_folders
from .store import BrainStore

__all__ = ["BrainStore", "index_configured_folders"]
