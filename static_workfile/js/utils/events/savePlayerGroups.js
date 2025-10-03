export const savePlayerGroups = function (socket) {
    const playerGroups = document.querySelectorAll('.player-group');
    const playerGroupData = [];

    for (const playerGroup of playerGroups) {
        const playerGroupTitle = playerGroup.querySelector('.player-group-title');
        const playerGroupPlayers = playerGroup.querySelectorAll('.player-selector');

        const playerGroupObject = {
            starting_type: playerGroupTitle.innerHTML,
            id: playerGroupTitle.id,
            players: [],
        };

        for (const player of playerGroupPlayers) {
            if (player.value) {
                playerGroupObject.players.push(player.value);
            } else {
                playerGroupObject.players.push(null);
            }
        }

        playerGroupData.push(playerGroupObject);
    }

    socket.send(
        JSON.stringify({
            command: 'savePlayerGroups',
            playerGroups: playerGroupData,
        }),
    );
};
