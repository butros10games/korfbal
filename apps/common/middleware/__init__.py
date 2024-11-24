"""Middleware package for the common app."""

from .visitor_tracking import VisitorTrackingMiddleware

__all__ = [
    "VisitorTrackingMiddleware",
]
