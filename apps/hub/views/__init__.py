"""This module contains the views for the hub app."""

from .catalog import catalog
from .catalog_data import catalog_data
from .index import index
from .previous_page import previous_page
from .search import search

__all__ = [
    "index",
    "catalog",
    "catalog_data",
    "search",
    "previous_page",
]
