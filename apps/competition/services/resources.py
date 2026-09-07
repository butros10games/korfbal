"""Verified collection endpoints and refresh intervals in hours."""

# Paths, parameter names and versions verified against actual app responses.
ENDPOINTS = {
    "club_logo": ("", None, 1, 87600),
    "clubs": ("club/Clubs", None, 1, 168),
    "club_teams": ("club/ClubTeams", "ClubId", 1, 168),
    "club_program": ("club/ClubProgram", "ClubId", 3, 24),
    "club_results": ("club/ClubMatchResults", "ClubId", 2, 168),
    "team_pools": ("team/TeamPoolAssignments", "PublicTeamId", 2, 168),
    "pool_results": ("pool/PoolCompetitionData", "PoolId", 2, 24),
}
