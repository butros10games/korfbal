import { sharedData } from '../../features/matches/sharedData.js';
import { getButtonBackground } from './scoringButtonUtils.js';

export const addPlayerClickHandlers = function (playerButtons, team, socket) {
    for (const element of playerButtons) {
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
    }
};

export const createPlayerClickHandler = function (element, team, socket) {
    return function () {
        const data = { command: 'get_goal_types' };
        const last_goal_Data = {
            player_id: element.id,
            for_team: team === 'home',
        };

        sharedData.last_goal_Data = last_goal_Data;
        sharedData.last_goal_Command = 'goal_reg';
        socket.send(JSON.stringify(data));
    };
};

export const removePlayerClickHandlers = function (playerButtons) {
    for (const element of playerButtons) {
        element.style.background = '';
        if (element.playerClickHandler) {
            element.removeEventListener('click', element.playerClickHandler);
            delete element.playerClickHandler;
        }
    }
};

export const shotButtonReg = function (team, socket) {
    const playerButtonsContainer = document.getElementById(
        team === 'home' ? 'Aanval' : 'Verdediging',
    );
    const playerButtons =
        playerButtonsContainer.getElementsByClassName('player-selector');

    // Remove event listeners from the deactivated button
    for (const element of playerButtons) {
        element.style.background = '';
        element.removeEventListener('click', element.playerClickHandler);
        delete element.playerClickHandler;

        // set a other click event to the player buttons to register shots
        const playerClickHandler = function () {
            // Ask for shot type so we can store shot quality for missed shots.
            // This remains optional: server accepts shot_reg without a shot_type.
            sharedData.last_goal_Data = {
                player_id: element.id,
                for_team: team === 'home',
            };
            sharedData.last_goal_Command = 'shot_reg';
            socket.send(JSON.stringify({ command: 'get_goal_types' }));
        };

        element.playerClickHandler = playerClickHandler;
        element.addEventListener('click', playerClickHandler);
    }
};
