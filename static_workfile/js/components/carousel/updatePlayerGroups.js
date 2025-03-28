import { truncateMiddle } from '../../utils/index.js';
import {
    createPlayerGroupContainer,
    createPlayerDiv,
    savePlayerGroups,
    onPlayerSelectChange,
} from '../../utils/events/index.js';

export const updatePlayerGroups = function (data, container, socket) {
    const playerOptions = data.players.map((dataPlayer) => {
        const option = document.createElement('option');
        option.value = dataPlayer.id;
        option.innerHTML = truncateMiddle(dataPlayer.name, 18);
        return option;
    });

    const playerGroupContainer = createPlayerGroupContainer(
        data.playerGroups,
        (player) => {
            const playerDiv = createPlayerDiv('select', player, playerOptions);
            playerDiv.addEventListener('change', function () {
                onPlayerSelectChange(this);
            });
            return playerDiv;
        },
    );

    const buttonDiv = document.createElement('div');
    buttonDiv.classList.add('flex-center');
    buttonDiv.style.marginTop = '12px';

    const saveButton = document.createElement('button');
    saveButton.id = 'saveButton';
    saveButton.innerHTML = 'Save';
    saveButton.style.display = 'none'; // Initially hidden
    buttonDiv.appendChild(saveButton);

    saveButton.addEventListener('click', () => {
        savePlayerGroups(socket);
    });

    playerGroupContainer.appendChild(buttonDiv);
    container.appendChild(playerGroupContainer);
};
