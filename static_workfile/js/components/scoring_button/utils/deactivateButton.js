import { getPlayerButtons } from "./getPlayerButtons.js";
import { getButtonBackground } from "./getButtonBackground.js";
import { removePlayerClickHandlers } from "./removePlayerClickHandlers.js";

export const deactivateButton = function(button, team) {
    const playerButtons = getPlayerButtons(team);
    button.style.background = getButtonBackground(team, false);
    button.classList.remove("activated");
    removePlayerClickHandlers(playerButtons);
};