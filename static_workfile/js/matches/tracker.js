let socket;
let match_id;
let WebSocket_url;
const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/g;
const url = window.location.href;

let eventsDiv;
let playersDiv;
let last_goal_Data;

let timer = null;

window.addEventListener("DOMContentLoaded", function() {
    eventsDiv = document.getElementById("match-event");
    playersDiv = document.getElementById("players");

    const matches = url.match(regex);

    console.log(matches);

    if (matches && matches.length >= 2) {
        firstUUID = matches[0];
        secondUUID = matches[1];
        console.log("First UUID:", firstUUID);
        console.log("Second UUID:", secondUUID);
    } else {
        console.log("Not enough UUIDs found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/match/tracker/" + firstUUID + "/" + secondUUID + "/";

    load_icon(eventsDiv);
    load_icon(playersDiv);
    initializeSocket(WebSocket_url);
    initializeButtons();
    scoringButtonSetup();
    startStopButtonSetup();
});

function initializeButtons() {
    const buttons = document.getElementsByClassName("button");
    for (const element of buttons) {
        element.addEventListener("click", function() {
            const data = {
                "command": "event",
                "event": element.id,
            }
            socket.send(JSON.stringify(data));
        });
    }

    const endHalfButton = document.getElementById("end-half-button");
    endHalfButton.addEventListener("click", function() {
        const data = {
            "command": "part_end",
        }
        socket.send(JSON.stringify(data));
    });
}

function startStopButtonSetup() {
    const startStopButton = document.getElementById("start-stop-button");

    startStopButton.addEventListener("click", function() {
        const data = {
            "command": "start/pause",
        }

        socket.send(JSON.stringify(data));
    });
}

function scoringButtonSetup() {
    const homeScoreButton = document.getElementById("home-score");
    const awayScoreButton = document.getElementById("away-score");

    function toggleButton(button, team) {
        const playerButtonsContainer = document.getElementById(team === "home" ? "Aanval" : "Verdediging");
        const playerButtons = playerButtonsContainer.getElementsByClassName("player-selector");
    
        if (button.classList.contains("activated")) {
            // Deactivate the button if it's already activated
            button.style.background = team === "home" ? "#43ff6480" : "rgba(235, 0, 0, 0.5)";
            button.classList.remove("activated");

            shotButtonReg(team);
        } else {
            // remove the playerClickHandler from the player buttons
            Array.from(playerButtons).forEach(element => {
                if (element._playerClickHandler) {
                    element.removeEventListener("click", element._playerClickHandler);
                    delete element._playerClickHandler;
                }
            });

            // First, deactivate the currently activated button (if any)
            const activatedButton = document.querySelector(".activated");
            if (activatedButton) {
                const otherTeam = activatedButton === homeScoreButton ? "home" : "away";
                activatedButton.style.background = otherTeam === "home" ? "#43ff6480" : "rgba(235, 0, 0, 0.5)";
                activatedButton.classList.remove("activated");
    
                // Remove event listeners from the previously activated button
                const otherPlayerButtonsContainer = document.getElementById(otherTeam === "home" ? "Aanval" : "Verdediging");
                const otherPlayerButtons = otherPlayerButtonsContainer.getElementsByClassName("player-selector");
                Array.from(otherPlayerButtons).forEach(element => {
                    element.style.background = "";
                    if (element._playerClickHandler) {
                        element.removeEventListener("click", element._playerClickHandler);
                        delete element._playerClickHandler;
                    }
                });
            }
    
            // Activate the pressed button
            button.style.background = team === "home" ? "#43ff64" : "rgba(235, 0, 0, 0.7)";
            button.classList.add("activated");
    
            // Apply changes and add event listeners to the pressed button
            Array.from(playerButtons).forEach(element => {
                element.style.background = team === "home" ? "#43ff6480" : "rgba(235, 0, 0, 0.5)";
    
                const playerClickHandler = function () {

                    const data = {
                        "command": "get_goal_types"
                    }

                    last_goal_Data = {
                        "player_id": element.id,
                        "time": new Date().toISOString(),
                        "for_team": team === "home" ? true : false,
                    }

                    socket.send(JSON.stringify(data));
                };
    
                element._playerClickHandler = playerClickHandler;
                element.addEventListener("click", playerClickHandler);
            });
        }
    }

    homeScoreButton.addEventListener("click", function () {
        toggleButton(homeScoreButton, "home");
    });

    awayScoreButton.addEventListener("click", function () {
        toggleButton(awayScoreButton, "away");
    });
}

function shotButtonReg(team) {
    const playerButtonsContainer = document.getElementById(team === "home" ? "Aanval" : "Verdediging");
    const playerButtons = playerButtonsContainer.getElementsByClassName("player-selector");
    
    // Remove event listeners from the deactivated button
    Array.from(playerButtons).forEach(element => {
        element.style.background = "";
        element.removeEventListener("click", element._playerClickHandler);
        delete element._playerClickHandler;

        // set a other click event to the player buttons to register shots
        const playerClickHandler = function () {
            const data = {
                "command": "shot_reg",
                "player_id": element.id,
                "time": new Date().toISOString(),
                "for_team": team === "home" ? true : false,
            }

            console.log(data);

            socket.send(JSON.stringify(data));
        };

        element._playerClickHandler = playerClickHandler;
        element.addEventListener("click", playerClickHandler);
    });
}

function requestInitalData() {
    socket.send(JSON.stringify({
        'command': 'playerGroups',
    }));

    socket.send(JSON.stringify({
        'command': 'last_event',
    }));

    socket.send(JSON.stringify({
        'command': 'get_time',
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

            shotButtonReg("home");
            shotButtonReg("away");
            break;

        case "player_shot_change":
            updatePlayerShot(data);
            break;

        case "goal_types":
            showGoalTypes(data);
            break;

        case "timer_data":
            if (timer) {
                return;
            }

            if (data.type === "active") {
                timer = new CountdownTimer(data.time, data.length * 1000, null, data.pause_length * 1000);
                timer.start();
            } else if (data.type === "pause") {
                timer = new CountdownTimer(data.time, data.length * 1000, data.calc_to, data.pause_length * 1000);
            } else if (data.type === "start") {
                timer = new CountdownTimer(data.time, data.length * 1000, null, 0);
                timer.start();
            }

            break;

        case "pause":
            if (data.pause === true) {
                timer.stop();
                console.log("Timer paused");
            } else if (data.pause === false) {
                timer.start(data.pause_time);
                console.log("Timer resumed");
            }

            break;

        case "team_goal_change":
            teamGoalChange(data);

            // remove overlay
            const overlay = document.getElementById("overlay");
            overlay.remove();

            // remove the collor change from the buttons
            const activatedButton = document.querySelector(".activated");
            if (activatedButton) {
                activatedButton.click();
            }

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

function teamGoalChange(data) {
    first_team = document.getElementById("home-score");
    firstTeamP = first_team.querySelector("p");

    second_team = document.getElementById("away-score");
    secondTeamP = second_team.querySelector("p");

    firstTeamP.innerHTML = data.goals_for;
    secondTeamP.innerHTML = data.goals_against;
}

function showGoalTypes(data) {
    // Create the overlay container
    const overlay = document.createElement("div");
    overlay.id = "overlay";
    overlay.classList.add("overlay");
    
    // Create the popup container
    const popup = document.createElement("div");
    popup.classList.add("popup");
    
    // Create the content for the popup
    const goalTypesContainer = document.createElement("div");
    goalTypesContainer.classList.add("goal-types-container");
    goalTypesContainer.style.display = "flex";
    goalTypesContainer.style.flexWrap = "wrap"; // Add this line to wrap the buttons to a second line

    const TopLineContainer = document.createElement("div");
    TopLineContainer.classList.add("flex-row");
    TopLineContainer.style.marginBottom = "12px";

    const goalTypesTitle = document.createElement("p");
    goalTypesTitle.innerHTML = "Doelpunt type";
    goalTypesTitle.style.margin = "0";

    TopLineContainer.appendChild(goalTypesTitle);

    // Create a close button for the popup
    const closeButton = document.createElement("button");
    closeButton.classList.add("close-button");
    closeButton.innerHTML = "Close";
    closeButton.addEventListener("click", function() {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();
    });

    TopLineContainer.appendChild(closeButton);

    goalTypesContainer.appendChild(TopLineContainer);

    for (const goalType of data.goal_types) {
        const goalTypeDiv = document.createElement("div");
        goalTypeDiv.classList.add("goal-type");
        goalTypeDiv.classList.add("flex-center");
        goalTypeDiv.style.flexGrow = "1";
        goalTypeDiv.style.flexBasis = "calc(50% - 32px)"; 
        goalTypeDiv.style.textAlign = "center";
        goalTypeDiv.style.margin = "0 12px 6px 12px";
        goalTypeDiv.style.width = "calc(100% - 12px)";
        goalTypeDiv.style.background = goalType.color;

        const goalTypeTitle = document.createElement("p");
        goalTypeTitle.classList.add("flex-center");
        goalTypeTitle.innerHTML = goalType.name;
        goalTypeTitle.style.margin = "0";
        goalTypeTitle.style.fontSize = "14px";
        goalTypeTitle.style.background = "var(--button-color)";
        goalTypeTitle.style.color = "var(--text-color)";
        goalTypeTitle.style.padding = "6px";
        goalTypeTitle.style.borderRadius = "4px";
        goalTypeTitle.style.width = "100%";
        goalTypeTitle.style.height = "42px";
        goalTypeTitle.style.cursor = "pointer";
        goalTypeTitle.style.userSelect = "none";

        goalTypeDiv.addEventListener("click", function() {
            const data = {
                "command": "goal_reg",
                "goal_type": goalType.id,
                "player_id": last_goal_Data.player_id,
                "time": last_goal_Data.time,
                "for_team": last_goal_Data.for_team,
            }

            socket.send(JSON.stringify(data));
        });

        goalTypeDiv.appendChild(goalTypeTitle);

        goalTypesContainer.appendChild(goalTypeDiv);
    }

    // Append the close button and goalTypesContainer to the popup
    popup.appendChild(goalTypesContainer);
    
    // Append the popup to the overlay
    overlay.appendChild(popup);
    
    // Append the overlay to the body to cover the entire screen
    document.body.appendChild(overlay);
    
    // Disable scrolling on the body while the overlay is open
    document.body.style.overflow = "hidden";
}

function updateEvent(data) {

}

function updatePlayerShot(data) {
    const playerGroups = document.getElementsByClassName("player-group-players");

    for (const playerGroup of playerGroups) {
        // Use attribute selector syntax
        const playerDiv = playerGroup.querySelector(`[id="${data.player_id}"]`);

        if (playerDiv) {
            playerDiv.querySelector("p:nth-child(2)").innerHTML = data.shots;
        }
    }
}

function showPlayerGroups(data) {
    let switchButton = false;
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
            playerGroupTitleDiv.style.marginBottom = "6px";
            playerGroupTitleDiv.style.margin = "0 12px 6px 12px";
            playerGroupTitleDiv.style.width = "calc(100% - 24px)";

            const playerGroupTitle = document.createElement("div");
            playerGroupTitle.style.fontWeight = "600";
            playerGroupTitle.innerHTML = playerGroup.starting_type;
            playerGroupTitle.id = playerGroup.id;

            playerGroupTitleDiv.appendChild(playerGroupTitle);

            if (!switchButton) {
                const switchButtonDiv = document.createElement("div");
                switchButtonDiv.innerHTML = "Wissel";
                switchButtonDiv.id = "switch-button";

                switchButtonDiv.classList.add("switch-button");
                switchButtonDiv.classList.add("flex-center");
                switchButtonDiv.style.width = "96px";

                switchButtonDiv.addEventListener("click", function() {
                    const data = {
                        "command": "switch",
                        "player_group": playerGroup.id,
                    }
                    socket.send(JSON.stringify(data));
                });

                playerGroupTitleDiv.appendChild(switchButtonDiv);

                switchButton = true;
            }

            const playerGroupPlayers = document.createElement("div");
            playerGroupPlayers.classList.add("player-group-players");
            playerGroupPlayers.classList.add("flex-row");
            playerGroupPlayers.style.flexWrap = "wrap";
            playerGroupPlayers.style.alignItems = 'stretch';

            playerGroupPlayers.id = playerGroup.starting_type;
        
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

                const playerShots = document.createElement("p");
                playerShots.style.margin = "0";
                playerShots.style.fontSize = "14px";

                if (player) {
                    playerDiv.id = player.id;
                    playerDiv.style.justifyContent = "space-around";

                    playerName.innerHTML = player.name;
                    playerShots.innerHTML = player.shots;
                } else {
                    playerName.innerHTML = "geen data";
                }

                playerDiv.appendChild(playerName);
                playerDiv.appendChild(playerShots);
        
                playerGroupPlayers.appendChild(playerDiv);
            }

            playerGroupDiv.appendChild(playerGroupTitleDiv);
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

class CountdownTimer {
    constructor(startTimeISO, lengthInMilliseconds, pauseTimeISO = null, offsetInMilliseconds = 0) {
        this.lengthInMilliseconds = lengthInMilliseconds;
        
        this.startTime = new Date(startTimeISO);
        this.totalLength = lengthInMilliseconds + offsetInMilliseconds;  // Include the offset
        this.endTime = new Date(this.startTime.getTime() + this.totalLength);
        
        this.offset = offsetInMilliseconds;
        this.pauseTimeISO = pauseTimeISO;
        this.interval = null;

        // Call updateDisplay immediately upon construction to set the initial value
        this.updateDisplay();
    }

    updateDisplay() {
        this.now = this.pauseTimeISO ? new Date(this.pauseTimeISO) : new Date();;
        let timeLeft = this.totalLength - (this.now - this.startTime);
    
        const sign = timeLeft < 0 ? '-' : '';
        const absTime = Math.abs(timeLeft);
    
        const minutes = Math.floor(absTime / 60000);
        const seconds = Math.floor((absTime % 60000) / 1000);
    
        // Update the counter display on the website
        document.getElementById('counter').innerText = `${sign}${minutes}:${seconds.toString().padStart(2, '0')}`;

        // if the time is under one minute add a end half button
        if (minutes < 1 || sign === "-") {
            const endHalfButton = document.getElementById("end-half-button");
            endHalfButton.style.display = "block";
        }
    }

    start(pause_time = null) {
        if (pause_time) {
            this.totalLength = this.lengthInMilliseconds + (pause_time * 1000);
        }

        this.pauseTimeISO = null;
        this.interval = setInterval(() => this.updateDisplay(), 1000);
    }

    stop() {
        clearInterval(this.interval);
        this.interval = null;
    }
}
