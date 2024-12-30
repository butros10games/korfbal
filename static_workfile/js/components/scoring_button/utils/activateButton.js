import { getPlayerButtons } from "./getPlayerButtons.js";
import { getButtonBackground } from "./getButtonBackground.js";
import { addPlayerClickHandlers } from "./addPlayerClickHandlers.js";

export const activateButton = function(button, team, socket) {
    const playerButtons = getPlayerButtons(team);
    button.style.background = getButtonBackground(team, true);
    button.classList.add("activated");
    addPlayerClickHandlers(playerButtons, team, socket);
};