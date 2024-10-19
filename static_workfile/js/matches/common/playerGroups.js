function createPlayerGroupTitle(playerGroup) {
    const playerGroupTitle = document.createElement("div");
    playerGroupTitle.classList.add("flex-row", "player-group-title");
    playerGroupTitle.style.justifyContent = "flex-start";
    playerGroupTitle.style.fontWeight = "600";
    playerGroupTitle.style.marginBottom = "6px";
    playerGroupTitle.style.marginLeft = "12px";
    playerGroupTitle.style.width = "calc(100% - 12px)";
    playerGroupTitle.innerHTML = playerGroup.starting_type;
    playerGroupTitle.id = playerGroup.id;
    return playerGroupTitle;
}

function createPlayerDiv(type, player, playerOptions = []) {
    const playerDiv = document.createElement(type === 'select' ? "select" : "div");
    playerDiv.classList.add("player-selector", "flex-row");
    playerDiv.style.flexGrow = "1";
    playerDiv.style.flexBasis = "calc(50% - 32px)";
    playerDiv.style.textAlign = "center";

    if (type === 'select') {
        playerOptions.forEach(option => {
            playerDiv.appendChild(option.cloneNode(true));
        });

        const nietIngevuldOption = document.createElement("option");
        nietIngevuldOption.value = NaN;
        nietIngevuldOption.innerHTML = 'Niet ingevuld';
        playerDiv.appendChild(nietIngevuldOption.cloneNode(true));

        if (player) {
            playerDiv.value = player.id;
        } else {
            playerDiv.value = NaN;
        }
    } else {
        const playerName = document.createElement("p");
        playerName.style.margin = "0";
        playerName.style.fontSize = "14px";
        playerName.innerHTML = player ? truncateMiddle(player.name, 16) : "geen data";
        playerDiv.appendChild(playerName);
    }

    return playerDiv;
}

function createPlayerGroupContainer(playerGroups, renderPlayerDiv) {
    const playerGroupContainer = document.createElement("div");
    playerGroupContainer.classList.add("player-group-container");

    if (playerGroups.length > 0) {
        playerGroups.forEach(playerGroup => {
            const playerGroupDiv = document.createElement("div");
            playerGroupDiv.classList.add("player-group", "flex-column");
            playerGroupDiv.style.marginTop = "12px";

            const playerGroupTitle = createPlayerGroupTitle(playerGroup);

            const playerGroupPlayers = document.createElement("div");
            playerGroupPlayers.classList.add("player-group-players", "flex-row");
            playerGroupPlayers.style.flexWrap = "wrap";
            playerGroupPlayers.style.alignItems = 'stretch';

            for (let i = 0; i < 4; i++) {
                let player = playerGroup.players[i];
                const playerDiv = renderPlayerDiv(player);
                playerGroupPlayers.appendChild(playerDiv);
            }

            playerGroupDiv.appendChild(playerGroupTitle);
            playerGroupDiv.appendChild(playerGroupPlayers);

            playerGroupContainer.appendChild(playerGroupDiv);
        });
    } else {
        const textElement = document.createElement("p");
        textElement.classList.add("flex-center");
        textElement.innerHTML = "<p>Geen spelersgroepen gevonden.</p>";
        playerGroupContainer.appendChild(textElement);
    }

    return playerGroupContainer;
}

window.updateplayerGroups = function(data, container) {
    const playerOptions = data.players.map(dataPlayer => {
        const option = document.createElement("option");
        option.value = dataPlayer.id;
        option.innerHTML = truncateMiddle(dataPlayer.name, 18);
        return option;
    });

    const playerGroupContainer = createPlayerGroupContainer(data.playerGroups, (player) => {
        const playerDiv = createPlayerDiv('select', player, playerOptions);
        playerDiv.addEventListener('change', function() {
            onPlayerSelectChange(this);
        });
        return playerDiv;
    });

    const buttonDiv = document.createElement("div");
    buttonDiv.classList.add("flex-center");
    buttonDiv.style.marginTop = "12px";

    const saveButton = document.createElement("button");
    saveButton.id = "saveButton";
    saveButton.innerHTML = "Save";
    saveButton.style.display = "none";  // Initially hidden
    buttonDiv.appendChild(saveButton);

    saveButton.addEventListener('click', function() {
        savePlayerGroups();
    });

    playerGroupContainer.appendChild(buttonDiv);
    container.appendChild(playerGroupContainer);
}

window.showPlayerGroups = function(data, container) {
    const playerGroupContainer = createPlayerGroupContainer(data.playerGroups, (player) => {
        return createPlayerDiv('div', player);
    });

    container.appendChild(playerGroupContainer);
}

window.savePlayerGroups = function() {
    const playerGroups = document.querySelectorAll('.player-group');
    const playerGroupData = [];

    playerGroups.forEach(playerGroup => {
        const playerGroupTitle = playerGroup.querySelector('.player-group-title');
        const playerGroupPlayers = playerGroup.querySelectorAll('.player-selector');

        const playerGroupObject = {
            'starting_type': playerGroupTitle.innerHTML,
            'id': playerGroupTitle.id,
            'players': []
        };

        playerGroupPlayers.forEach(player => {
            if (player.value) {
                playerGroupObject.players.push(player.value);
            } else {
                playerGroupObject.players.push(null);
            }
        });

        playerGroupData.push(playerGroupObject);
    });

    socket.send(JSON.stringify({
        'command': 'savePlayerGroups',
        'playerGroups': playerGroupData
    }));
}
