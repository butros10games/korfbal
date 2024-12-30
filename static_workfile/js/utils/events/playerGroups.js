import { truncateMiddle } from "..";

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

export const createPlayerDiv = function(type, player, playerOptions = []) {
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
        playerName.style.fontSize = "16px";
        playerName.innerHTML = player ? truncateMiddle(player.name, 16) : "geen data";
        playerDiv.appendChild(playerName);
    }

    return playerDiv;
};

export const createPlayerGroupContainer = function(playerGroups, renderPlayerDiv) {
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
                const player = playerGroup.players[i];
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
};
