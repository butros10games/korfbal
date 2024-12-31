export const getPlayerButtons = function(team) {
    const containerId = team === 'home' ? 'Aanval' : 'Verdediging';
    const playerButtonsContainer = document.getElementById(containerId);
    return playerButtonsContainer.getElementsByClassName('player-selector');
};
