"""M contains the views for the hub app."""

from .catalog import catalog
from .catalog_data import catalog_data
from .index import index
from .previous_page import previous_page
from .search import search
from .service_worker import service_worker


__all__ = [
    "catalog",
    "catalog_data",
    "index",
    "previous_page",
    "search",
    "service_worker",
]
