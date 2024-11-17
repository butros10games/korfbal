import { deactivateButton } from "./deactivate_button.js";

export const deactivateActivatedButton = function(homeScoreButton) {
    const activatedButton = document.querySelector(".activated");
    if (activatedButton) {
        const team = activatedButton === homeScoreButton ? "home" : "away";
        deactivateButton(activatedButton, team);
    }
};