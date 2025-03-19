export const savePlayerGroups = function (socket) {
    const playerGroups = document.querySelectorAll('.player-group');
    const playerGroupData = [];

    playerGroups.forEach((playerGroup) => {
        const playerGroupTitle = playerGroup.querySelector('.player-group-title');
        const playerGroupPlayers = playerGroup.querySelectorAll('.player-selector');

        const playerGroupObject = {
            starting_type: playerGroupTitle.innerHTML,
            id: playerGroupTitle.id,
            players: [],
        };

        playerGroupPlayers.forEach((player) => {
            if (player.value) {
                playerGroupObject.players.push(player.value);
            } else {
                playerGroupObject.players.push(null);
            }
        });

        playerGroupData.push(playerGroupObject);
    });

    socket.send(
        JSON.stringify({
            command: 'savePlayerGroups',
            playerGroups: playerGroupData,
        }),
    );
};
