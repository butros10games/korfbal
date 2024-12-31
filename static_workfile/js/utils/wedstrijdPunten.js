export const wedstrijdPunten = function(event, thuis, uit, home_team_id) {
    if (event.team_id === home_team_id) {
        thuis++;
    } else {
        uit++;
    }
    return [thuis, uit];
};
