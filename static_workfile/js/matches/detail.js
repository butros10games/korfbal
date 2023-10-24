let socket;
let match_id;
let WebSocket_url;
let infoContainer;
let user_id;
const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;

let touchStartX = 0;
let touchEndX = 0;
let isDragging = false;
let currentPosition = 0;

let buttonWidth;
let carousel;

window.addEventListener("DOMContentLoaded", function() {
    buttonWidth = document.querySelector('.button').offsetWidth;
    carousel = document.querySelector('.carousel');

    infoContainer = document.getElementById("info-container");
    user_id = document.getElementById("user_id").innerText;
    
    const matches = url.match(regex);

    if (matches) {
        match_id = matches[1];
        console.log(match_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/match/" + match_id + "/";

    load_icon();
    initializeSocket(WebSocket_url);
    setNavButtons();
});

function setNavButtons() {
    // Button selection for the carousel
    const buttons = document.querySelectorAll(".button");
    buttons.forEach((button) => {
        button.addEventListener("click", function () {
            // Deactivate the other buttons
            buttons.forEach((element) => {
                element.classList.remove("active");
            });

            this.classList.add("active");

            // Get data out of the button
            const data = this.getAttribute("data");

            socket.send(JSON.stringify({
                'command': data,
                'user_id': user_id
            }));

            cleanDom();
            load_icon();
        });
    });

    // Touch event handlers
    carousel.addEventListener('touchstart', (e) => {
        touchStartX = e.touches[0].clientX;
        startPosition = currentPosition;
        isDragging = true;
        carousel.style.transition = 'none';
    });

    carousel.addEventListener('touchmove', (e) => {
        if (!isDragging) return;
        touchEndX = e.touches[0].clientX;
        const diff = touchEndX - touchStartX;
        currentPosition = startPosition + diff;
        carousel.style.transform = `translateX(${currentPosition}px)`;
    });

    carousel.addEventListener('touchend', () => {
        if (!isDragging) return;
        const diff = touchEndX - touchStartX;

        if (diff > buttonWidth / 3) {
            // Swipe to the right, go to the previous item
            currentPosition += buttonWidth;
        } else if (diff < -buttonWidth / 3) {
            // Swipe to the left, go to the next item
            currentPosition -= buttonWidth;
        }

        // Ensure the carousel doesn't go beyond the boundaries
        currentPosition = Math.max(currentPosition, -(carousel.scrollWidth - carousel.clientWidth));
        currentPosition = Math.min(currentPosition, 0);

        carousel.style.transition = 'transform 0.3s ease';
        carousel.style.transform = `translateX(${currentPosition}px)`;

        isDragging = false;
    });
}

function requestInitalData() {
    button = document.querySelector(".button.active");
    var data = button.getAttribute('data');

    socket.send(JSON.stringify({
        'command': data,
        'user_id': user_id
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
        case "events":
            cleanDom();

            updateEvents(data);
            break;
        
        case "playerGroups":
            cleanDom();

            if (data.is_coach) {
                updateplayerGroups(data);
            } else {
                showPlayerGroups(data);
            }
            break;
    }
}

function load_icon() {
    infoContainer.classList.add("flex-center");
    infoContainer.innerHTML = "<div id='load_icon' class='lds-ring'><div></div><div></div><div></div><div></div></div>";
}

function cleanDom() {
    infoContainer.innerHTML = "";
    infoContainer.classList.remove("flex-center");
    infoContainer.classList.remove("flex-start-wrap");
}

function updateEvents(data) {
    const events = data.events;

    let thuis = 0
    let uit = 0

    const eventContainer = document.createElement("div");
    eventContainer.classList.add("event-container");

    if (events.length > 0) {
        events.forEach(event => {
            const eventDiv = document.createElement("div");
            eventDiv.classList.add("event");
            eventDiv.classList.add("flex-row");

            if (event.type == "goal") {
                const eventTypeDiv = document.createElement("div");
                eventTypeDiv.classList.add("event-type");
                eventTypeDiv.innerHTML = event.type;
                if (event.for_team) {
                    eventTypeDiv.style.backgroundColor = '#4CAF50';
                    thuis++;
                } else {
                    eventTypeDiv.style.backgroundColor = '#red';
                    uit++;
                }

                const midsectionDiv = document.createElement("div");
                midsectionDiv.classList.add("flex-column");

                const descriptionDiv = document.createElement("div");
                descriptionDiv.classList.add("description");
                descriptionDiv.innerHTML = event.goal_type + " (" + event.time + "\")";

                const playerName = document.createElement("p");
                playerName.innerHTML = event.player;
                playerName.style.margin = "0";

                midsectionDiv.appendChild(descriptionDiv);
                midsectionDiv.appendChild(playerName);

                const currentScoreDiv = document.createElement("div");
                currentScoreDiv.classList.add("current-score");
                currentScoreDiv.innerHTML = thuis + "-" + uit;

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(currentScoreDiv);
            } else if (event.type == "substitution") {

            } else if (event.type == "pause") {

            }

            eventContainer.appendChild(eventDiv);
        });
    } else {
        const textElement = document.createElement("p");
        textElement.classList.add("flex-center");
        textElement.innerHTML = "<p>Geen events gevonden.</p>";

        eventContainer.appendChild(textElement);
    }

    if (data.access && !data.finished) {
        const buttonContainer = document.createElement("div");
        buttonContainer.classList.add("flex-center");
        buttonContainer.style.marginTop = "12px";

        const trackerButton = document.createElement("a");
        trackerButton.classList.add("tracker-button");
        trackerButton.href = "/match/tracker/" + match_id + "/";
        trackerButton.innerHTML = "bijhouden";

        buttonContainer.appendChild(trackerButton);
        eventContainer.appendChild(buttonContainer);
    }

    infoContainer.appendChild(eventContainer);
}

function onPlayerSelectChange(changedSelect) {
    const allSelectors = document.querySelectorAll('.player-selector');
    allSelectors.forEach(select => {
        // Skip the select that was changed
        if (select === changedSelect) return;

        // If another select has the same value, reset it
        if (select.value === changedSelect.value) {
            select.value = NaN;  // Set to 'Niet ingevuld' value
        }
    });

    // Show the save button
    document.getElementById("saveButton").style.display = "block";
}

function savePlayerGroups() {
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
        'user_id': user_id,
        'playerGroups': playerGroupData
    }));
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

            const playerGroupTitle = document.createElement("div");
            playerGroupTitle.classList.add("flex-row");
            playerGroupTitle.classList.add("player-group-title");
            playerGroupTitle.style.justifyContent = "flex-start";
            playerGroupTitle.style.fontWeight = "600";
            playerGroupTitle.style.marginBottom = "6px";
            playerGroupTitle.style.marginLeft = "12px";
            playerGroupTitle.style.width = "calc(100% - 12px)";
            playerGroupTitle.innerHTML = playerGroup.starting_type;
            playerGroupTitle.id = playerGroup.id;

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

    infoContainer.appendChild(playerGroupContainer);
}

function updateplayerGroups(data) {
    const playerGroups = data.playerGroups;

    const playerGroupContainer = document.createElement("div");
    playerGroupContainer.classList.add("player-group-container");

    if (playerGroups.length > 0) {
        playerGroups.forEach(playerGroup => {
            const playerGroupDiv = document.createElement("div");
            playerGroupDiv.classList.add("player-group");
            playerGroupDiv.classList.add("flex-column");
            playerGroupDiv.style.marginTop = "12px";

            const playerGroupTitle = document.createElement("div");
            playerGroupTitle.classList.add("flex-row");
            playerGroupTitle.classList.add("player-group-title");
            playerGroupTitle.style.justifyContent = "flex-start";
            playerGroupTitle.style.fontWeight = "600";
            playerGroupTitle.style.marginBottom = "6px";
            playerGroupTitle.style.marginLeft = "12px";
            playerGroupTitle.style.width = "calc(100% - 12px)";
            playerGroupTitle.innerHTML = playerGroup.starting_type;
            playerGroupTitle.id = playerGroup.id;

            const playerGroupPlayers = document.createElement("div");
            playerGroupPlayers.classList.add("player-group-players");
            playerGroupPlayers.classList.add("flex-row");
            playerGroupPlayers.style.flexWrap = "wrap";
            playerGroupPlayers.style.alignItems = 'stretch';

            const playerOptions = data.players.map(dataPlayer => {
                const option = document.createElement("option");
                option.value = dataPlayer.id;
                option.innerHTML = dataPlayer.name;
                return option;
            });
        
            const nietIngevuldOption = document.createElement("option");
            nietIngevuldOption.value = NaN;
            nietIngevuldOption.innerHTML = 'Niet ingevuld';
        
            for (let i = 0; i < 4; i++) {
                let player = playerGroup.players[i];
        
                const playerDiv = document.createElement("select");
                playerDiv.classList.add("player-selector", "flex-row");
                playerDiv.style.flexGrow = "1";
                playerDiv.style.flexBasis = "calc(50% - 32px)"; 
                playerDiv.style.textAlign = "center";
        
                // Attach the event listener
                playerDiv.addEventListener('change', function() {
                    onPlayerSelectChange(this);
                });
        
                // Append the list of player options
                playerOptions.forEach(option => {
                    playerDiv.appendChild(option.cloneNode(true));  // Clone the option to avoid moving the same node
                });
        
                // Append the 'Niet ingevuld' option
                playerDiv.appendChild(nietIngevuldOption.cloneNode(true));
        
                // If a player is already selected, set the value of the dropdown
                if (player) {
                    playerDiv.value = player.id;
                } else {
                    playerDiv.value = NaN;  // Set to 'Niet ingevuld' value
                }
        
                playerGroupPlayers.appendChild(playerDiv);
            }

            playerGroupDiv.appendChild(playerGroupTitle);
            playerGroupDiv.appendChild(playerGroupPlayers);

            playerGroupContainer.appendChild(playerGroupDiv);
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
    } else {
        const textElement = document.createElement("p");
        textElement.classList.add("flex-center");
        textElement.innerHTML = "<p>Geen spelersgroepen gevonden.</p>";

        playerGroupContainer.appendChild(textElement);
    }

    infoContainer.appendChild(playerGroupContainer);
}