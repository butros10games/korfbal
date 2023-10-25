let socket;
let match_id;
let WebSocket_url;
const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;

let eventsDiv;
let playersDiv;

window.addEventListener("DOMContentLoaded", function() {
    eventsDiv = document.getElementById("match-event");
    playersDiv = document.getElementById("players");

    const matches = url.match(regex);

    if (matches) {
        match_id = matches[1];
        console.log(match_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/match/tracker/" + match_id + "/";

    load_icon(eventsDiv);
    load_icon(playersDiv);
    initializeSocket(WebSocket_url);
    initializeButtons();
});

function initializeButtons() {
    const buttons = document.getElementsByClassName("button");
    for (let i = 0; i < buttons.length; i++) {
        buttons[i].addEventListener("click", function() {
            const data = {
                "command": "event",
                "event": buttons[i].id,
            }
            socket.send(JSON.stringify(data));
        });
    }
}

function requestInitalData() {
    socket.send(JSON.stringify({
        'command': 'playerGroups',
    }));

    socket.send(JSON.stringify({
        'command': 'events',
    }));
}

// Function to initialize WebSocket
function initializeSocket(url) {
    // Close the current connection if it exists
    if (socket) {
        socket.onclose = null; // Clear the onclose handler to prevent console error logging
        socket.close();
    }
    
    // Create a new WebSocket connection
    socket = new WebSocket(url);
    
    // On successful connection
    socket.onopen = function(e) {
        console.log("Connection established!");
        requestInitalData();
    };
    
    // On message received
    socket.onmessage = onMessageReceived;
    
    // On connection closed
    socket.onclose = function(event) {
        if (event.wasClean) {
            console.log(`Connection closed cleanly, code=${event.code}, reason=${event.reason}`);
        } else {
            console.error('Connection died');
        }

        console.log("Attempting to reconnect...");
        // Attempt to reconnect
        setTimeout(() => initializeSocket(url), 3000);
    };
}

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    console.log(data);

    switch(data.command) {
        case "event":
            cleanDom(eventsDiv);
            cleanDom(playersDiv);

            updateEvent(data);
            break;
        
        case "playerGroups":
            cleanDom(eventsDiv);
            cleanDom(playersDiv);

            showPlayerGroups(data);
            break;
    }
}

function load_icon(element) {
    element.classList.add("flex-center");
    element.innerHTML = "<div id='load_icon' class='lds-ring'><div></div><div></div><div></div><div></div></div>";
}

function cleanDom(element) {
    element.innerHTML = "";
    element.classList.remove("flex-center");
    element.classList.remove("flex-start-wrap");
}

function showPlayerGroups(data) {
    const playerGroups = data.playerGroups;

    const playerGroupContainer = document.createElement("div");
    playerGroupContainer.classList.add("player-group-container");

    if (playerGroups.length > 0) {
        playerGroups.forEach(playerGroup => {
            const playerGroupDiv = document.createElement("div");
            playerGroupDiv.classList.add("player-group");
            playerGroupDiv.classList.add("flex-column");
            playerGroupDiv.style.marginTop = "12px";

            const playerGroupTitleDiv = document.createElement("div");
            playerGroupTitleDiv.classList.add("flex-row");
            playerGroupTitleDiv.classList.add("player-group-title");
            playerGroupTitleDiv.style.justifyContent = "flex-start";
            playerGroupTitleDiv.style.marginBottom = "6px";
            playerGroupTitleDiv.style.marginLeft = "12px";
            playerGroupTitleDiv.style.width = "calc(100% - 12px)";

            const playerGroupTitle = document.createElement("div");
            playerGroupTitle.style.fontWeight = "600";
            playerGroupTitle.innerHTML = playerGroup.starting_type;
            playerGroupTitle.id = playerGroup.id;

            playerGroupTitleDiv.appendChild(playerGroupTitle);

            if (!document.getElementById("switch-button")) {
                const switchButton = document.createElement("div");
                switchButton.innerHTML = "Wissel";
                switchButton.addEventListener("click", function() {
                    const data = {
                        "command": "switch",
                        "player_group": playerGroup.id,
                    }
                    socket.send(JSON.stringify(data));
                });

                playerGroupTitleDiv.appendChild(switchButton);
            }

            const playerGroupPlayers = document.createElement("div");
            playerGroupPlayers.classList.add("player-group-players");
            playerGroupPlayers.classList.add("flex-row");
            playerGroupPlayers.style.flexWrap = "wrap";
            playerGroupPlayers.style.alignItems = 'stretch';
        
            for (let i = 0; i < 4; i++) {
                let player = playerGroup.players[i];
        
                const playerDiv = document.createElement("div");
                playerDiv.classList.add("player-selector", "flex-center");
                playerDiv.style.flexGrow = "1";
                playerDiv.style.flexBasis = "calc(50% - 32px)"; 
                playerDiv.style.textAlign = "center";

                const playerName = document.createElement("p");
                playerName.style.margin = "0";
                playerName.style.fontSize = "14px";

                if (player) {
                    playerName.innerHTML = player.name;
                } else {
                    playerName.innerHTML = "geen data";
                }

                playerDiv.appendChild(playerName);
        
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

    playersDiv.appendChild(playerGroupContainer);
}