"use strict";

import { createPlayerGroupContainer, createPlayerDiv } from "./events_utils";

export const showPlayerGroups = function(data, container) {
    const playerGroupContainer = createPlayerGroupContainer(data.playerGroups, 
        (player) => {
            return createPlayerDiv('div', player);
        }
    );

    container.appendChild(playerGroupContainer);
};