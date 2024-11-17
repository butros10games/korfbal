import { activateButton } from "./activate_button.js";
import { deactivateButton } from "./deactivate_button.js";
import { deactivateActivatedButton } from "./deactivate_activated_button.js";
import { shotButtonReg } from "./shot_button_reg.js";

export const toggleButton = function(button, team, socket, homeScoreButton) {
    if (button.classList.contains("activated")) {
        deactivateButton(button, team);
        shotButtonReg(team, socket);
    } else {
        deactivateActivatedButton(homeScoreButton);
        activateButton(button, team, socket);
    }
};