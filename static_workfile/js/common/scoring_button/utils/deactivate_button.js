import { getPlayerButtons } from "./get_player_buttons.js";
import { getButtonBackground } from "./get_button_background.js";
import { removePlayerClickHandlers } from "./remove_player_click_handlers.js";

export const deactivateButton = function(button, team) {
    const playerButtons = getPlayerButtons(team);
    button.style.background = getButtonBackground(team, false);
    button.classList.remove("activated");
    removePlayerClickHandlers(playerButtons);
};