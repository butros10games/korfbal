import { createPlayerGroupContainer, createPlayerDiv } from '../../utils/events';

export const showPlayerGroups = function (data, container) {
    const playerGroupContainer = createPlayerGroupContainer(
        data.playerGroups,
        (player) => createPlayerDiv('div', player),
    );

    container.appendChild(playerGroupContainer);
};
