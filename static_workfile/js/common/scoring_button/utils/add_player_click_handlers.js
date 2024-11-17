import { getButtonBackground } from "./get_button_background.js";
import { createPlayerClickHandler } from "./create_player_click_handler.js";

export const addPlayerClickHandlers = function(playerButtons, team, socket) {
    Array.from(playerButtons).forEach(element => {
        element.style.background = getButtonBackground(team, false);

        // If a previous handler exists, remove it
        if (element.playerClickHandler) {
            element.removeEventListener("click", element.playerClickHandler);
            delete element.playerClickHandler;
        }

        // Create a new handler and store it in playerClickHandler
        const playerClickHandler = createPlayerClickHandler(element, team, socket);
        element.playerClickHandler = playerClickHandler;
        element.addEventListener("click", playerClickHandler);
    });
};