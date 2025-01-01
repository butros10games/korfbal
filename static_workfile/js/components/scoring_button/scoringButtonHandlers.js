import { sharedData } from '../../../features/matches/sharedData.js';
import { getButtonBackground } from './getButtonBackground.js';

export const addPlayerClickHandlers = function(playerButtons, team, socket) {
    Array.from(playerButtons).forEach(element => {
        element.style.background = getButtonBackground(team, false);

        // If a previous handler exists, remove it
        if (element.playerClickHandler) {
            element.removeEventListener('click', element.playerClickHandler);
            delete element.playerClickHandler;
        }

        // Create a new handler and store it in playerClickHandler
        const playerClickHandler = createPlayerClickHandler(element, team, socket);
        element.playerClickHandler = playerClickHandler;
        element.addEventListener('click', playerClickHandler);
    });
};

export const createPlayerClickHandler = function(element, team, socket) {
    return function() {
        const data = { 'command': 'get_goal_types' };
        const last_goal_Data = {
            'player_id': element.id,
            'time': new Date().toISOString(),
            'for_team': team === 'home',
        };

        sharedData.last_goal_Data = last_goal_Data;
        socket.send(JSON.stringify(data));
    };
};


export const removePlayerClickHandlers = function(playerButtons) {
    Array.from(playerButtons).forEach(element => {
        element.style.background = '';
        if (element.playerClickHandler) {
            element.removeEventListener('click', element.playerClickHandler);
            delete element.playerClickHandler;
        }
    });
};


export const shotButtonReg = function(team, socket) {
    const playerButtonsContainer = document.getElementById(team === 'home' ? 'Aanval' : 'Verdediging');
    const playerButtons = playerButtonsContainer.getElementsByClassName('player-selector');

    // Remove event listeners from the deactivated button
    Array.from(playerButtons).forEach(element => {
        element.style.background = '';
        element.removeEventListener('click', element.playerClickHandler);
        delete element.playerClickHandler;

        // set a other click event to the player buttons to register shots
        const playerClickHandler = function() {
            const data = {
                'command': 'shot_reg',
                'player_id': element.id,
                'time': new Date().toISOString(),
                'for_team': team === 'home',
            };

            console.log(data);

            socket.send(JSON.stringify(data));
        };

        element.playerClickHandler = playerClickHandler;
        element.addEventListener('click', playerClickHandler);
    });
};
