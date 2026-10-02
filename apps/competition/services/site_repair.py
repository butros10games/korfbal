"""Remove result-site matches that duplicate provider poules and read the site again.

A site fills only poules the provider did not deliver. An import that compared
one season instead of the whole edition copied full-year outdoor poules the
provider keeps in their own season. The repair removes those copies, with the
fixtures they created unless those carry native activity, and queues the site
again so every row is placed by the current rules.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from apps.competition.models import HistoricalResource, Match
from apps.competition.services.full_year_repair import remove_orphan_teams
from apps.competition.services.history import ARCHIVE_PREFIX, SITE_PROVIDERS
from apps.competition.services.history_editions import edition_scopes
from apps.competition.services.history_sites import (
    NAMESPACES,
    covered_pools,
    remove_site_matches,
)


def repair_site(provider: str, edition: int, *, apply: bool) -> dict[str, Any]:
    """Preview, or remove, one site's copies of provider poules in an edition.

    Returns:
        The duplicate matches and poules found and, when applied, what changed.

    Raises:
        ValueError: The source is not a public result site.

    """
    if provider not in SITE_PROVIDERS:
        raise ValueError("Unknown public result site")
    scopes = edition_scopes(edition)
    site = Match.objects.filter(
        season__in=scopes,
        external_id__startswith=f"{ARCHIVE_PREFIX}{NAMESPACES[provider]}:",
    )
    covered = covered_pools(
        scopes, set(site.exclude(pool=None).values_list("pool__external_id", flat=True))
    )
    duplicates = site.filter(pool__external_id__in=covered)
    result: dict[str, Any] = {
        "edition": edition,
        "source": provider,
        "duplicates": duplicates.count(),
        "poules": len(covered),
    }
    if not apply:
        return result
    kept: set[str] = set()
    # One transaction per poule keeps the locks short beside the running import.
    for pool in sorted(set(duplicates.values_list("pool_id", flat=True))):
        kept |= remove_site_matches(duplicates.filter(pool_id=pool))
    result["kept_in_use"] = len(kept)
    result["removed"] = result["duplicates"] - len(kept)
    result.update(remove_orphan_teams(scopes))
    result["requeued"] = (
        HistoricalResource.objects
        .filter(season__in=scopes, provider=provider)
        .exclude(state="pending")
        .update(state="pending", reason="", attempts=0, next_attempt_at=timezone.now())
    )
    return result
