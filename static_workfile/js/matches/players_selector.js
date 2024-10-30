let selectedPlayers = [];
let groupId = null;
let csrfToken = null;
let matchId = null;
let teamId = null;
let playersGroupsData = [];

const groupTypes = {
    "01927d88-6878-7d0f-acbf-28c251fbc2b5": [{ text: "Remove", func: changePlayerGroup, id: "0192c7bb-c664-77fd-8a29-acde6f428c93" }], // attack
    "01927d88-57ef-7339-a791-67cf856bfea1": [{ text: "Remove", func: changePlayerGroup, id: "0192c7bb-c664-77fd-8a29-acde6f428c93" }], // defense
    "0192c7bb-c664-77fd-8a29-acde6f428c93": [
        { text: "Remove", func: changePlayerGroup },
        { text: "Attack", func: changePlayerGroup, id: "01927d88-6878-7d0f-acbf-28c251fbc2b5", player_max: 4 },
        { text: "Defense", func: changePlayerGroup, id: "01927d88-57ef-7339-a791-67cf856bfea1", player_max: 4 }
    ] // reserve
};

const typeIds = [
    "01927d88-6878-7d0f-acbf-28c251fbc2b5",
    "01927d88-57ef-7339-a791-67cf856bfea1",
    "0192c7bb-c664-77fd-8a29-acde6f428c93"
];

const groupIdsToTypeIds = {};
const TypeIdsToGroupIds = {};

window.addEventListener("DOMContentLoaded", initialize);

function initialize() {
    csrfToken = document.getElementsByName("csrfmiddlewaretoken")[0].value;
    [matchId, teamId] = parseUrl();
    mapGroupIdsToTypeIds();
    mapTypeIdsToGroupIds();
    getplayersGroupsData();
    setupPlayerButtons();
}

function parseUrl() {
    const urlParts = window.location.href.split("/");
    return [urlParts[urlParts.length - 3], urlParts[urlParts.length - 2]];
}

function getplayersGroupsData() {
    const playerGroup = document.querySelectorAll(".player-group");
    playerGroup.forEach(group => {
        const playersData = [];

        group.querySelectorAll(".player").forEach(player => {
            playersData.push({ playerId: player.id }); 
        });

        playersGroupsData.push({ groupId: group.id, playersData: playersData });
    });

    console.log(playersGroupsData);
}

function setupPlayerButtons() {
    document.querySelectorAll(".player").forEach(player => {
        player.addEventListener("click", () => handlePlayerSelection(player));
    });
}

function handlePlayerSelection(player) {
    const localGroupId = player.parentElement?.id || null;

    if (groupId === localGroupId || groupId === null) {
        togglePlayerSelection(player, localGroupId);
    }
}

function togglePlayerSelection(player, localGroupId) {
    const playerId = player.id;
    const playerIndex = selectedPlayers.findIndex(p => p.playerId === playerId);

    if (playerIndex === -1) {
        selectedPlayers.push({ playerId, groupId: localGroupId });
        groupId = localGroupId;
    } else {
        selectedPlayers.splice(playerIndex, 1);
        if (selectedPlayers.length === 0) groupId = null;
    }
    highlightSelectedPlayer(player);
    updateOptionsBar();
}

function updateOptionsBar() {
    if (selectedPlayers.length > 0) {
        if (!document.getElementById("options-bar")) {
            showOptionsBar();
        }
    } else {
        removeOptionsBar();
    }
}

function highlightSelectedPlayer(player) {
    player.style.backgroundColor = selectedPlayers.some(p => p.playerId === player.id) ? "lightblue" : "white";
}

function showOptionsBar() {
    const optionsBar = document.createElement("div");
    optionsBar.classList.add("flex-row", "options-bar");
    optionsBar.id = "options-bar";

    const options = groupTypes[groupIdsToTypeIds[groupId]];
    options.forEach(option => {
        const button = document.createElement("button");
        button.innerText = option.text;
        button.addEventListener("click", () => {
            const playersGroupData = playersGroupsData.find(group => group.groupId === TypeIdsToGroupIds[option.id]);
            const numberPlayersGroup = playersGroupData.playersData.length;

            if (option.player_max && numberPlayersGroup + selectedPlayers.length > option.player_max) {
                alert(`Je hebt te veel spelers geslecteed voor de group. Group max: ${option.player_max}.`);
                return;
            }

            option.func(selectedPlayers, TypeIdsToGroupIds[option.id] || null);
            removeOptionsBar();
        });
        optionsBar.appendChild(button);
    });

    document.body.appendChild(optionsBar);
    adjustScrollableHeight("calc(100vh - 208px)");
}

function removeOptionsBar() {
    const optionsBar = document.getElementById("options-bar");
    if (optionsBar) {
        optionsBar.remove();
        adjustScrollableHeight();
    }
}

function adjustScrollableHeight(height = "") {
    document.querySelector(".scrollable").style.height = height;
}

function mapGroupIdsToTypeIds() {
    typeIds.forEach(typeId => {
        const groupElement = document.getElementById(typeId);
        if (groupElement) groupIdsToTypeIds[groupElement.innerText] = typeId;
    });
}

function mapTypeIdsToGroupIds() {
    typeIds.forEach(typeId => {
        const groupElement = document.getElementById(typeId);
        if (groupElement) TypeIdsToGroupIds[typeId] = groupElement.innerText;
    });
}

function changePlayerGroup(selectedPlayers, newGroupId) {
    fetchData(`/match/api/player_designation/`, {
        players: selectedPlayers,
        new_group_id: newGroupId
    });
}

async function fetchData(url, bodyData) {
    try {
        const response = await fetch(url, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken
            },
            body: JSON.stringify(bodyData)
        });
        if (response.ok) console.log("Player group updated successfully.");
        else throw new Error("Error updating player group.");
    } catch (error) {
        console.error(error.message);
    }
}
