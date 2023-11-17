let socket;
let match_id;
let WebSocket_url;
const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/g;
const url = window.location.href;

let eventsDiv;
let playersDiv;
let last_goal_Data;

let timer = null;

let playerGroupsData;
let playerSwitchData;

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

    if (data.error) {
        if (data.error === "match is paused") {
            // give a notification like popup that the match is paused
            const overlay = document.createElement("div");
            overlay.id = "overlay";
            overlay.classList.add("overlay");
            
            // Create the popup container
            const popup = document.createElement("div");
            popup.classList.add("popup");

            const popupText = document.createElement("p");
            popupText.innerHTML = "De wedstrijd is gepauzeerd.";
            popupText.style.margin = "0";
            popupText.style.fontSize = "18px";
            popupText.style.fontWeight = "600";
            popupText.style.marginBottom = "12px";

            popup.appendChild(popupText);

            const popupButton = document.createElement("button");
            popupButton.classList.add("button");
            popupButton.innerHTML = "OK";
            popupButton.style.margin = "0";
            popupButton.style.width = "100%";
            popupButton.style.height = "42px";
            popupButton.style.fontSize = "14px";
            popupButton.style.fontWeight = "600";
            popupButton.style.marginBottom = "12px";
            popupButton.style.background = "var(--button-color)";
            popupButton.style.color = "var(--text-color)";
            popupButton.style.border = "none";
            popupButton.style.borderRadius = "4px";
            popupButton.style.cursor = "pointer";
            popupButton.style.userSelect = "none";

            popupButton.addEventListener("click", function() {
                // Remove the popup and overlay when the close button is clicked
                overlay.remove();

                // remove the scroll lock
                document.body.style.overflow = "";
            });

            popup.appendChild(popupButton);

            // Append the popup to the overlay
            overlay.appendChild(popup);

            // Append the overlay to the body to cover the entire screen
            document.body.appendChild(overlay);

            // Disable scrolling on the body while the overlay is open
            document.body.style.overflow = "hidden";
        }
        return;
    }

    switch(data.command) {
        case "last_event":
            cleanDom(eventsDiv);

            updateEvent(data);
            break;
        
        case "playerGroups":
            cleanDom(eventsDiv);
            cleanDom(playersDiv);

            playerGroupsData = data;

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

                // set the pause button to pause
                const startStopButton = document.getElementById("start-stop-button");
                startStopButton.innerHTML = "Pause";

            } else if (data.type === "pause") {
                timer = new CountdownTimer(data.time, data.length * 1000, data.calc_to, data.pause_length * 1000);

                // set the pause button to start
                const startStopButton = document.getElementById("start-stop-button");
                startStopButton.innerHTML = "Start";

            } else if (data.type === "start") {
                timer = new CountdownTimer(data.time, data.length * 1000, null, 0);
                timer.start();
            }

            break;

        case "pause":
            if (data.pause === true) {
                timer.stop();
                console.log("Timer paused");

                // set the pause button to start
                const startStopButton = document.getElementById("start-stop-button");
                startStopButton.innerHTML = "Start";

            } else if (data.pause === false) {
                timer.start(data.pause_time);
                console.log("Timer resumed");

                // set the pause button to pause
                const startStopButton = document.getElementById("start-stop-button");
                startStopButton.innerHTML = "Pause";
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

        case "non_active_players":
            showReservePlayer(data);

            break;

        case "player_change":
            playerChange(data);
        
            break;

        case "part_end":
            periode_p = document.getElementById("periode_number");
            periode_p.innerHTML = data.part;

            // reset the timer
            timer.stop();

            // destroy the timer
            timer = null;

            let timer_p = document.getElementById("counter");
            
            // convert seconds to minutes and seconds
            minutes = data.part_length / 60;
            seconds = data.part_length % 60;
            
            timer_p.innerHTML = minutes + ":" + seconds.toString().padStart(2, '0');

            // hide the end half button
            const endHalfButton = document.getElementById("end-half-button");
            endHalfButton.style.display = "none";

            break;

        case "match_end":
            // remove the timer
            if (timer) {
                timer.stop();
                timer = null;
            }

            // set the pause button to start
            const startStopButton = document.getElementById("start-stop-button");
            startStopButton.innerHTML = "match ended";

            // add a overlay with the match end and a button when pressed it goes back to the match detail page
            const overlay_ended = document.createElement("div");
            overlay_ended.id = "overlay";
            overlay_ended.classList.add("overlay");

            // Create the popup container
            const popup = document.createElement("div");
            popup.classList.add("popup");

            const popupText = document.createElement("p");
            popupText.innerHTML = "De wedstrijd is afgelopen.";
            popupText.style.margin = "0";
            popupText.style.fontSize = "18px";
            popupText.style.fontWeight = "600";
            popupText.style.marginBottom = "12px";

            popup.appendChild(popupText);

            const popupButton = document.createElement("button");
            popupButton.classList.add("button");
            popupButton.innerHTML = "OK";
            popupButton.style.margin = "0";
            popupButton.style.width = "100%";
            popupButton.style.height = "42px";
            popupButton.style.fontSize = "14px";
            popupButton.style.fontWeight = "600";
            popupButton.style.marginBottom = "12px";
            popupButton.style.background = "var(--button-color)";
            popupButton.style.color = "var(--text-color)";
            popupButton.style.border = "none";
            popupButton.style.borderRadius = "4px";
            popupButton.style.cursor = "pointer";
            popupButton.style.userSelect = "none";

            popupButton.addEventListener("click", function() {
                // Remove the popup and overlay when the close button is clicked
                overlay_ended.remove();

                // remove the scroll lock
                document.body.style.overflow = "";

                // go back to the match detail page
                window.location.href = "/match/" + data.match_id + "/";
            });

            popup.appendChild(popupButton);

            // Append the popup to the overlay
            overlay_ended.appendChild(popup);

            // Append the overlay to the body to cover the entire screen
            document.body.appendChild(overlay_ended);

            // Disable scrolling on the body while the overlay is open
            document.body.style.overflow = "hidden";

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

function playerChange(data) {
    // look for the player button
    const playerButtonData = document.getElementById(data.player_out_id);

    playerButtonData.id = data.player_in_id;
    playerButtonData.querySelector("p").innerHTML = data.player_in;
    
    // change the player shot registration points
    shots_for = playerButtonData.querySelector("#shots-for");
    shots_against = playerButtonData.querySelector("#shots-against");

    shots_for.innerHTML = data.player_in_shots_for;
    shots_against.innerHTML = data.player_in_shots_against;

    playerSwitch()

    // remove the overlay
    const overlay = document.getElementById("overlay");
    overlay.remove();
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

function showReservePlayer(data) {
    // Create the overlay container
    const overlay = document.createElement("div");
    overlay.id = "overlay";
    overlay.classList.add("overlay");
    
    // Create the popup container
    const popup = document.createElement("div");
    popup.classList.add("popup");
    
    // Create the content for the popup
    const PlayersContainer = document.createElement("div");
    PlayersContainer.classList.add("goal-types-container");
    PlayersContainer.style.display = "flex";
    PlayersContainer.style.flexWrap = "wrap"; // Add this line to wrap the buttons to a second line

    const TopLineContainer = document.createElement("div");
    TopLineContainer.classList.add("flex-row");
    TopLineContainer.style.marginBottom = "12px";

    const PlayersTitle = document.createElement("p");
    PlayersTitle.innerHTML = "Doelpunt type";
    PlayersTitle.style.margin = "0";

    TopLineContainer.appendChild(PlayersTitle);

    // Create a close button for the popup
    const closeButton = document.createElement("button");
    closeButton.classList.add("close-button");
    closeButton.innerHTML = "Close";
    closeButton.addEventListener("click", function() {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();
    });

    TopLineContainer.appendChild(closeButton);

    PlayersContainer.appendChild(TopLineContainer);

    for (const Player of data.players) {
        const PlayerDiv = document.createElement("div");
        PlayerDiv.classList.add("goal-type");
        PlayerDiv.classList.add("flex-center");
        PlayerDiv.style.flexGrow = "1";
        PlayerDiv.style.flexBasis = "calc(50% - 32px)"; 
        PlayerDiv.style.textAlign = "center";
        PlayerDiv.style.margin = "0 12px 6px 12px";
        PlayerDiv.style.width = "calc(100% - 12px)";
        PlayerDiv.style.background = Player.color;

        const PlayerTitle = document.createElement("p");
        PlayerTitle.classList.add("flex-center");
        PlayerTitle.innerHTML = Player.name;
        PlayerTitle.style.margin = "0";
        PlayerTitle.style.fontSize = "14px";
        PlayerTitle.style.background = "var(--button-color)";
        PlayerTitle.style.color = "var(--text-color)";
        PlayerTitle.style.padding = "6px";
        PlayerTitle.style.borderRadius = "4px";
        PlayerTitle.style.width = "100%";
        PlayerTitle.style.height = "42px";
        PlayerTitle.style.cursor = "pointer";
        PlayerTitle.style.userSelect = "none";

        PlayerDiv.addEventListener("click", function() {
            const data = {
                "command": "wissel_reg",
                "new_player_id": Player.id,
                "old_player_id": playerSwitchData.player_id,
                "time": playerSwitchData.time,
            }

            console.log(data);

            socket.send(JSON.stringify(data));
        });

        PlayerDiv.appendChild(PlayerTitle);

        PlayersContainer.appendChild(PlayerDiv);
    }

    // Append the close button and PlayersContainer to the popup
    popup.appendChild(PlayersContainer);
    
    // Append the popup to the overlay
    overlay.appendChild(popup);
    
    // Append the overlay to the body to cover the entire screen
    document.body.appendChild(overlay);
    
    // Disable scrolling on the body while the overlay is open
    document.body.style.overflow = "hidden";
}

function updateEvent(data) {
    switch(data.event) {
        case "no_event":
            const textElement = document.createElement("p");
            textElement.classList.add("flex-center");
            textElement.style.margin = "0";
            textElement.style.height = "64px";
            textElement.innerHTML = "<p>Geen events gevonden.</p>";

            eventsDiv.appendChild(textElement);
            break;

        case "goal":
            const goalElement = document.createElement("p");
            goalElement.classList.add("flex-center");
            goalElement.style.margin = "0";
            goalElement.style.height = "64px";
            goalElement.innerHTML = "<p>Doelpunt.</p>";

            eventsDiv.appendChild(goalElement);
            break;

        case "shot":
            const shotElement = document.createElement("p");
            shotElement.classList.add("flex-center");
            shotElement.style.margin = "0";
            shotElement.style.height = "64px";
            shotElement.innerHTML = "<p>Schot.</p>";

            eventsDiv.appendChild(shotElement);
            break;

        case "shot_against":
            const shotAgainstElement = document.createElement("p");
            shotAgainstElement.classList.add("flex-center");
            shotAgainstElement.style.margin = "0";
            shotAgainstElement.style.height = "64px";
            shotAgainstElement.innerHTML = "<p>Schot tegen.</p>";

            eventsDiv.appendChild(shotAgainstElement);
            break;
    }
}

function updatePlayerShot(data) {
    const playerGroups = document.getElementsByClassName("player-group-players");

    for (const playerGroup of playerGroups) {
        // Use attribute selector syntax
        const playerDiv = playerGroup.querySelector(`[id="${data.player_id}"]`);

        if (playerDiv) {
            const shotsFor = playerDiv.querySelector("#shots-for");
            const shotsAgainst = playerDiv.querySelector("#shots-against");

            shotsFor.innerHTML = data.shots_for;
            shotsAgainst.innerHTML = data.shots_against;
        }
    }
}

function showPlayerGroups(data) {
    const homeScoreButton = document.getElementById("home-score");
    const awayScoreButton = document.getElementById("away-score");

    // remove the activated class from the buttons and remove the color
    if (homeScoreButton.classList.contains("activated")) {
        homeScoreButton.classList.remove("activated");

        homeScoreButton.style.background = "#43ff6480";
    }

    if (awayScoreButton.classList.contains("activated")) {
        awayScoreButton.classList.remove("activated");

        awayScoreButton.style.background = "rgba(235, 0, 0, 0.5)";
    }

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
            playerGroupTitle.innerHTML = playerGroup.current_type;
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
                    playerSwitch();
                });

                playerGroupTitleDiv.appendChild(switchButtonDiv);

                switchButton = true;
            }

            const playerGroupPlayers = document.createElement("div");
            playerGroupPlayers.classList.add("player-group-players");
            playerGroupPlayers.classList.add("flex-row");
            playerGroupPlayers.style.flexWrap = "wrap";
            playerGroupPlayers.style.alignItems = 'stretch';

            playerGroupPlayers.id = playerGroup.current_type;
        
            for (let i = 0; i < 4; i++) {
                let player = playerGroup.players[i];
        
                const playerDiv = document.createElement("div");
                playerDiv.classList.add("player-selector", "flex-center");
                playerDiv.style.flexGrow = "1";
                playerDiv.style.flexBasis = "calc(50% - 44px)"; 
                playerDiv.style.padding = "0 6px";
                playerDiv.style.textAlign = "center";

                const playerName = document.createElement("p");
                playerName.style.margin = "0";
                playerName.style.fontSize = "14px";

                const playerShots = document.createElement("div");
                playerShots.classList.add("flex-column");

                const playerShotsfor = document.createElement("p");
                playerShotsfor.id = "shots-for";
                playerShotsfor.style.margin = "0";
                playerShotsfor.style.fontSize = "14px";
                playerShotsfor.style.marginBottom = "-10px";

                const playerShotsAgainst = document.createElement("p");
                playerShotsAgainst.id = "shots-against";
                playerShotsAgainst.style.margin = "0";
                playerShotsAgainst.style.fontSize = "14px";
                playerShotsAgainst.style.marginTop = "-10px";

                if (player) {
                    playerDiv.id = player.id;
                    playerDiv.style.justifyContent = "space-between";


                    playerName.innerHTML = player.name;
                    playerShotsfor.innerHTML = player.shots_for;
                    playerShotsAgainst.innerHTML = player.shots_against;
                } else {
                    playerName.innerHTML = "geen data";
                }

                const playerShotsDivider = document.createElement("p");
                playerShotsDivider.style.margin = "0";
                playerShotsDivider.style.fontSize = "14px";
                playerShotsDivider.innerHTML = "-";

                playerShots.appendChild(playerShotsfor);
                playerShots.appendChild(playerShotsDivider);
                playerShots.appendChild(playerShotsAgainst);

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

function playerSwitch() {
    // add to the wissel button a active tag and change the color
    const switchButton = document.getElementById("switch-button");
    
    if (switchButton.classList.contains("activated")) {
        switchButton.classList.remove("activated");
        switchButton.style.background = "";

        // remove the color from the player buttons and remove the event listeners
        const playerButtons = document.getElementsByClassName("player-selector");

        Array.from(playerButtons).forEach(element => {
            element.style.background = "";
            if (element._playerClickHandler) {
                element.removeEventListener("click", element._playerClickHandler);
                delete element._playerClickHandler;
            }
        });

        shotButtonReg("home");
        shotButtonReg("away");
    } else {
        switchButton.classList.add("activated");
        switchButton.style.background = "#4169e152";

        // change the color on the player buttons and remove the event listeners and add a event listener to the player buttons to switch the player clicked on
        const playerButtons = document.getElementsByClassName("player-selector");

        Array.from(playerButtons).forEach(element => {
            const homeScoreButton = document.getElementById("home-score");
            const awayScoreButton = document.getElementById("away-score");

            // remove the activated class from the buttons and remove the color
            if (homeScoreButton.classList.contains("activated")) {
                homeScoreButton.classList.remove("activated");

                homeScoreButton.style.background = "#43ff6480";
            }

            if (awayScoreButton.classList.contains("activated")) {
                awayScoreButton.classList.remove("activated");

                awayScoreButton.style.background = "rgba(235, 0, 0, 0.5)";
            }

            element.style.background = "#4169e152";
            if (element._playerClickHandler) {
                element.removeEventListener("click", element._playerClickHandler);
                delete element._playerClickHandler;
            }

            const playerClickHandler = function () {
                playerSwitchData = {
                    "player_id": element.id,
                    "time": new Date().toISOString(),
                }

                const data = {
                    "command": "get_non_active_players"
                }

                socket.send(JSON.stringify(data));
            };

            element._playerClickHandler = playerClickHandler;
            element.addEventListener("click", playerClickHandler);
        });
    }
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