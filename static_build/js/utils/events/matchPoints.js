export const matchPoints = function (event, home, away, home_team_id) {
    if (event.team_id === home_team_id) {
        home++;
    } else {
        away++;
    }
    return [home, away];
};
