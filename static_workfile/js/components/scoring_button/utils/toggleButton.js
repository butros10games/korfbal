import { activateButton } from "./activateButton.js";
import { deactivateButton } from "./deactivateButton.js";
import { deactivateActivatedButton } from "./deactivateActivatedButton.js";
import { shotButtonReg } from "./shotButtonReg.js";

export const toggleButton = function(button, team, socket, homeScoreButton) {
    if (button.classList.contains("activated")) {
        deactivateButton(button, team);
        shotButtonReg(team, socket);
    } else {
        deactivateActivatedButton(homeScoreButton);
        activateButton(button, team, socket);
    }
};