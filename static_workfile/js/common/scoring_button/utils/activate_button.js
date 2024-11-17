import { getPlayerButtons } from "./get_player_buttons.js";
import { getButtonBackground } from "./get_button_background.js";
import { addPlayerClickHandlers } from "./add_player_click_handlers.js";

export const activateButton = function(button, team, socket) {
    const playerButtons = getPlayerButtons(team);
    button.style.background = getButtonBackground(team, true);
    button.classList.add("activated");
    addPlayerClickHandlers(playerButtons, team, socket);
};