"""Verified collection endpoints and refresh intervals in hours."""

# One initial attempt plus at most five retries per consecutive failure streak.
MAX_FEED_FAILURES = 6

# Paths, parameter names and versions verified against actual app responses.
ENDPOINTS = {
    "player_photo": ("", None, 1, 87600),
    "club_logo": ("", None, 1, 87600),
    "clubs": ("club/Clubs", None, 1, 168),
    "club_teams": ("club/ClubTeams", "ClubId", 1, 168),
    "club_program": ("club/ClubProgram", "ClubId", 3, 24),
    "club_results": ("club/ClubMatchResults", "ClubId", 2, 168),
    "team_roster": ("team/TeamPersons", "PublicTeamId", 0, 168),
    "team_pools": ("team/TeamPoolAssignments", "PublicTeamId", 2, 168),
    "pool_results": ("pool/PoolCompetitionData", "PoolId", 2, 24),
}
