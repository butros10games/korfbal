import { shotButtonReg } from './shotButtonReg.js';
import { removePlayerClickHandlers } from './removePlayerClickHandlers.js';
import { addPlayerClickHandlers } from './addPlayerClickHandlers.js';

export const toggleButton = function (button, team, socket, homeScoreButton) {
    if (button.classList.contains('activated')) {
        deactivateButton(button, team);
        shotButtonReg(team, socket);
    } else {
        deactivateActivatedButton(homeScoreButton);
        activateButton(button, team, socket);
    }
};

export const getButtonBackground = function (team, isActive) {
    if (team === 'home') {
        return isActive ? '#43ff64' : '#43ff6480';
    } else {
        return isActive ? 'rgba(235, 0, 0, 0.7)' : 'rgba(235, 0, 0, 0.5)';
    }
};

const deactivateButton = function (button, team) {
    const playerButtons = getPlayerButtons(team);
    button.style.background = getButtonBackground(team, false);
    button.classList.remove('activated');
    removePlayerClickHandlers(playerButtons);
};

const deactivateActivatedButton = function (homeScoreButton) {
    const activatedButton = document.querySelector('.activated');
    if (activatedButton) {
        const team = activatedButton === homeScoreButton ? 'home' : 'away';
        deactivateButton(activatedButton, team);
    }
};

const activateButton = function (button, team, socket) {
    const playerButtons = getPlayerButtons(team);
    button.style.background = getButtonBackground(team, true);
    button.classList.add('activated');
    addPlayerClickHandlers(playerButtons, team, socket);
};

const getPlayerButtons = function (team) {
    const containerId = team === 'home' ? 'Aanval' : 'Verdediging';
    const playerButtonsContainer = document.getElementById(containerId);
    return playerButtonsContainer.getElementsByClassName('player-selector');
};
