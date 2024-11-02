let socket;
let firstUUID;
let secondUUID;
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

    load_icon_small(eventsDiv);
    load_icon(playersDiv);
    initializeSocket(WebSocket_url);
    initializeButtons();
    scoringButtonSetup();
    startStopButtonSetup();

    setupSwipeDelete()
    deleteButtonSetup()
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
        if (button.classList.contains("activated")) {
            deactivateButton(button, team);
            shotButtonReg(team);
        } else {
            deactivateActivatedButton();
            activateButton(button, team);
        }
    }

    function deactivateButton(button, team) {
        const playerButtons = getPlayerButtons(team);
        button.style.background = getButtonBackground(team, false);
        button.classList.remove("activated");
        removePlayerClickHandlers(playerButtons);
    }

    function deactivateActivatedButton() {
        const activatedButton = document.querySelector(".activated");
        if (activatedButton) {
            const team = activatedButton === homeScoreButton ? "home" : "away";
            deactivateButton(activatedButton, team);
        }
    }

    function activateButton(button, team) {
        const playerButtons = getPlayerButtons(team);
        button.style.background = getButtonBackground(team, true);
        button.classList.add("activated");
        addPlayerClickHandlers(playerButtons, team);
    }

    function getPlayerButtons(team) {
        const containerId = team === "home" ? "Aanval" : "Verdediging";
        const playerButtonsContainer = document.getElementById(containerId);
        return playerButtonsContainer.getElementsByClassName("player-selector");
    }

    function getButtonBackground(team, isActive) {
        if (team === "home") {
            return isActive ? "#43ff64" : "#43ff6480";
        } else {
            return isActive ? "rgba(235, 0, 0, 0.7)" : "rgba(235, 0, 0, 0.5)";
        }
    }

    function removePlayerClickHandlers(playerButtons) {
        Array.from(playerButtons).forEach(element => {
            element.style.background = "";
            if (element._playerClickHandler) {
                element.removeEventListener("click", element._playerClickHandler);
                delete element._playerClickHandler;
            }
        });
    }

    function addPlayerClickHandlers(playerButtons, team) {
        Array.from(playerButtons).forEach(element => {
            element.style.background = getButtonBackground(team, false);
    
            // Remove existing event listener
            if (element._playerClickHandler) {
                element.removeEventListener("click", element._playerClickHandler);
                delete element._playerClickHandler;
            }
    
            const playerClickHandler = createPlayerClickHandler(element, team);
            element._playerClickHandler = playerClickHandler;
            element.addEventListener("click", playerClickHandler);
        });
    }

    function createPlayerClickHandler(element, team) {
        return function () {
            const data = { "command": "get_goal_types" };
            last_goal_Data = {
                "player_id": element.id,
                "time": new Date().toISOString(),
                "for_team": team === "home",
            };
            socket.send(JSON.stringify(data));
        };
    }

    homeScoreButton.addEventListener("click", () => toggleButton(homeScoreButton, "home"));
    awayScoreButton.addEventListener("click", () => toggleButton(awayScoreButton, "away"));
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
                "for_team": team === "home",
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
    const startStopButton = document.getElementById("start-stop-button");

    if (data.error) {
        errorProcessing(data);
        return;
    }

    switch(data.command) {
        case "last_event": {
            lastEvent(data);
            break;
        }
        
        case "playerGroups": {
            playerGroups(data);
            break;
        }

        case "player_shot_change": {
            updatePlayerShot(data);
            break;
        }

        case "goal_types": {
            showGoalTypes(data);
            break;
        }

        case "timer_data": {
            timerData(data, startStopButton);
            break;
        }

        case "pause": {
            pauseTimer(data, startStopButton);
            break;
        }

        case "team_goal_change": {
            teamGoalChangeFunction(data);
            break;
        }

        case "non_active_players": {
            showReservePlayer(data);
            break;
        }

        case "player_change": {
            playerChange(data);
            break;
        }

        case "part_end": {
            partEnd(data, startStopButton);
            break;
        }

        case "match_end": {
            matchEnd(data, startStopButton);
            break;
        }
    }
}

function errorProcessing(data) {
    if (data.error === "match is paused") {
        // give a notification like popup that the match is paused
        const overlay = document.createElement("div");
        overlay.id = "overlay";
        overlay.classList.add("overlay");
        
        const popupElements = createPopup("De wedstrijd is gepauzeerd.");
        const popup = popupElements[0];
        const popupButton = popupElements[1];

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
}

function lastEvent(data) {
    cleanDom(eventsDiv);
    resetSwipe()
    
    updateEvent(data);
}

function playerGroups(data) {
    cleanDom(eventsDiv);
    cleanDom(playersDiv);

    playerGroupsData = data;

    if (data.match_active) {
        showPlayerGroups(data);

        shotButtonReg("home");
        shotButtonReg("away");
    } else {
        updateplayerGroups(data, playersDiv); // imported from matches/common/updateplayerGroups.js
    }
}

function timerData(data, startStopButton) {
    // remove the timer if it exists
    if (timer) {
        timer.destroy();
        timer = null;
    }

    if (data.type === "active") {
        timer = new CountdownTimer(data.time, data.length * 1000, null, data.pause_length * 1000, true);
        timer.start();

        // set the pause button to pause
        startStopButton.innerHTML = "Pause";

    } else if (data.type === "pause") {
        timer = new CountdownTimer(data.time, data.length * 1000, data.calc_to, data.pause_length * 1000, true);
        timer.stop();

        // set the pause button to start
        startStopButton.innerHTML = "Start";

    } else if (data.type === "start") {
        timer = new CountdownTimer(data.time, data.length * 1000, null, 0, true);
        timer.start();

        startStopButton.innerHTML = "Pause";
    }
}

function pauseTimer(data, startStopButton) {
    if (data.pause === true) {
        timer.stop();
        console.log("Timer paused");

        // set the pause button to start
        startStopButton.innerHTML = "Start";

    } else if (data.pause === false) {
        timer.start(data.pause_time);
        console.log("Timer resumed");

        // set the pause button to pause
        startStopButton.innerHTML = "Pause";
    }
}

function teamGoalChangeFunction(data) {
    teamGoalChange(data);

    // remove overlay
    const overlay = document.getElementById("overlay");
    if (overlay) {
        overlay.remove();
    }

    // remove the collor change from the buttons
    const activatedButton = document.querySelector(".activated");
    if (activatedButton) {
        activatedButton.click();
    }
}

function partEnd(data, startStopButton) {
    const periode_p = document.getElementById("periode_number");
    periode_p.innerHTML = data.part;

    // reset the timer
    timer.stop();

    // destroy the timer
    timer = null;

    let timer_p = document.getElementById("counter");
    
    // convert seconds to minutes and seconds
    const minutes = data.part_length / 60;
    const seconds = data.part_length % 60;
    
    timer_p.innerHTML = minutes + ":" + seconds.toString().padStart(2, '0');

    // hide the end half button
    const endHalfButton = document.getElementById("end-half-button");
    endHalfButton.style.display = "none";

    // change the start/pause button to start
    startStopButton.innerHTML = "start";
}

function matchEnd(data, startStopButton) {
    // remove the timer
    if (timer) {
        timer.stop();
        timer = null;
    }

    // set the pause button to start
    startStopButton.innerHTML = "match ended";

    // add a overlay with the match end and a button when pressed it goes back to the match detail page
    const overlay_ended = document.createElement("div");
    overlay_ended.id = "overlay";
    overlay_ended.classList.add("overlay");

    const popupElements = createPopup("De wedstrijd is afgelopen.");
    const popup = popupElements[0];
    const popupButton = popupElements[1];

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
}

function createPopup(popupTextData) {
    // Create the popup container
    const popup = document.createElement("div");
    popup.classList.add("popup");

    const popupText = document.createElement("p");
    popupText.innerHTML = popupTextData;
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

    return [popup, popupButton];
}

function load_icon(element) {
    element.classList.add("flex-center");
    element.innerHTML = "<div id='load_icon' class='lds-ring'><div></div><div></div><div></div><div></div></div>";
}

function load_icon_small(element) {
    element.classList.add("flex-center");
    element.innerHTML = "<div id='load_icon' class='small-lds-ring'><div></div><div></div><div></div><div></div></div>";
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
    const shots_for = playerButtonData.querySelector("#shots-for");
    const shots_against = playerButtonData.querySelector("#shots-against");

    shots_for.innerHTML = data.player_in_shots_for;
    shots_against.innerHTML = data.player_in_shots_against;

    playerSwitch()

    // remove the overlay
    const overlay = document.getElementById("overlay");
    overlay.remove();
}

function teamGoalChange(data) {
    const first_team = document.getElementById("home-score");
    const firstTeamP = first_team.querySelector("p");

    const second_team = document.getElementById("away-score");
    const secondTeamP = second_team.querySelector("p");

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
    const event = data.last_event;
    const eventsDiv = document.createElement("div");
    eventsDiv.style.display = "flex";
    eventsDiv.style.justifyContent = 'space-between';
    eventsDiv.style.width = "100%";
    eventsDiv.style.height = "100%";

    switch (event.type) {
        case "goal": {
            const eventTypeDiv = createEventTypeDiv(event.type, "64px", event.for_team ? '#4CAF50' : 'rgba(235, 0, 0, 0.7)');
            const midsectionDiv = createMidsectionDiv(event.shot_type + " (\"" + event.time + "\")", truncateMiddle(event.player, 20));
            const scoreDiv = createScoreDiv(event.goals_for + "-" + event.goals_against, "84px");

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(scoreDiv);
            break;
        }
        case "wissel": {
            const eventTypeDiv = createEventTypeDiv(event.type, "64px", '#eb9834');
            const midsectionDiv = createMidsectionDiv("(\"" + event.time + "\")", truncateMiddle(event.player_in, 15) + " --> " + truncateMiddle(event.player_out, 15));
            const endSectionDiv = document.createElement("div");
            endSectionDiv.style.width = "84px";  // For spacing/alignment purposes

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(endSectionDiv);
            break;
        }
        case "pause": {
            const eventTypeDiv = createEventTypeDiv(event.type, "64px", '#2196F3');
            const midsectionDiv = createMidsectionDiv("(\"" + event.time + "\")", getFormattedTime(event));
            const endSectionDiv = document.createElement("div");
            endSectionDiv.style.width = "84px";  // For spacing/alignment purposes

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(endSectionDiv);
            break;
        }
        case "shot": {
            const eventTypeDiv = createEventTypeDiv(event.type, "64px", event.for_team ? '#43ff644d' : '#eb00004d');
            const midsectionDiv = createMidsectionDiv("(\"" + event.time + "\")", truncateMiddle(event.player, 20));
            const endSectionDiv = document.createElement("div");
            endSectionDiv.style.width = "84px";  // For spacing/alignment purposes

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(endSectionDiv);
            break;
        }
        case "no_event": {
            const textElement = document.createElement("p");
            textElement.classList.add("flex-center");
            textElement.style.margin = "0";
            textElement.style.height = "64px";
            textElement.innerHTML = "Geen events gevonden.";
            textElement.style.width = "100%";

            eventsDiv.appendChild(textElement);
            break;
        }
        default: {
            console.warn("Unknown event type: ", event.type);
            const defaultElement = document.createElement("p");
            defaultElement.innerHTML = "Onbekend event type: " + event.type;
            eventsDiv.appendChild(defaultElement);
            break;
        }
    }

    // Append eventsDiv to the container (assuming there's a container in the DOM to append it to)
    const eventContainer = document.getElementById("match-event"); // Replace with the actual container ID
    if (eventContainer) {
        eventContainer.appendChild(eventsDiv);
    } else {
        console.error("Event container not found");
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

                const playerShotsDivider = document.createElement("p");
                playerShotsDivider.style.margin = "0";
                playerShotsDivider.style.fontSize = "14px";

                if (player) {
                    playerDiv.id = player.id;
                    playerDiv.style.justifyContent = "space-between";


                    playerName.innerHTML = truncateMiddle(player.name, 16);
                    playerShotsfor.innerHTML = player.shots_for;
                    playerShotsAgainst.innerHTML = player.shots_against;

                    playerShotsDivider.innerHTML = "-";
                } else {
                    playerName.innerHTML = "geen data";
                }

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

function setupSwipeDelete() {
    const matchEvent = document.getElementById('match-event-swipe');
    const swipeContent = document.getElementById('match-event');
    swipeContent.style.transform = `translateX(0px)`;
    let startX, currentX, isSwiping = false;

    const onTouchStart = (e) => {
        const transform = window.getComputedStyle(swipeContent).getPropertyValue('transform');
        const transformX = transform.split(',')[4].trim();

        startX = e.touches[0].clientX - parseInt(transformX);
        isSwiping = true;
        swipeContent.classList.remove('transition-back');
    };

    const onTouchMove = (e) => {
        if (!isSwiping) return;

        currentX = e.touches[0].clientX;
        const distance = startX - currentX;
        if (distance >= 0) {
            requestAnimationFrame(() => {
                swipeContent.style.transform = `translateX(${-Math.min(distance, 100)}px)`;
            });
        }
    };

    const onTouchEnd = () => {
        isSwiping = false;
        const swipeDistance = startX - currentX;
        const isSwipeLeft = swipeDistance > 50;
        swipeContent.style.transform = isSwipeLeft ? 'translateX(-100px)' : 'translateX(0px)';
        swipeContent.classList.add('transition-back');
        matchEvent.classList.toggle('swiped-left', isSwipeLeft);
    };

    swipeContent.addEventListener('touchstart', onTouchStart, { passive: true });
    swipeContent.addEventListener('touchmove', onTouchMove, { passive: true });
    swipeContent.addEventListener('touchend', onTouchEnd, false);
}

function resetSwipe() {
    // Assuming swipeContent is the element you want to reset
    const swipeContent = document.getElementById('match-event');

    // Reset transform to initial state
    swipeContent.style.transform = 'translateX(0px)';

    // Reset any classes that might have been added or removed during swipe
    swipeContent.classList.remove('transition-back');
    swipeContent.classList.remove('swiped-left'); // If this class is added on swipe
}

function deleteButtonSetup() {
    const deleteButton = document.getElementById('deleteButton');

    deleteButton.addEventListener('click', () => {
        deleteConfirmPopup();
    });
}

function deleteConfirmPopup() {
    // Create the overlay container
    const overlay = document.createElement("div");
    overlay.id = "overlay";
    overlay.classList.add("overlay");

    // Create the popup container
    const popup = document.createElement("div");
    popup.classList.add("popup");

    const popupTextRow = document.createElement("div");
    popupTextRow.classList.add("flex-row");
    popupTextRow.style.marginBottom = "24px";

    // Create the content for the popup
    const popupText = document.createElement("p");
    popupText.innerHTML = "Event verwijderen?";
    popupText.style.margin = "0";
    popupText.style.fontSize = "18px";
    popupText.style.fontWeight = "600";

    // Create a close button for the popup
    const closeButton = document.createElement("button");
    closeButton.classList.add("close-button");
    closeButton.innerHTML = "Close";
    closeButton.addEventListener("click", function() {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();
    });

    popupTextRow.appendChild(popupText);
    popupTextRow.appendChild(closeButton);

    popup.appendChild(popupTextRow);

    const popupButtonContainer = document.createElement("div");
    popupButtonContainer.classList.add("flex-row");
    popupButtonContainer.style.justifyContent = "space-between";

    const popupButton = document.createElement("button");
    popupButton.classList.add("button");
    popupButton.innerHTML = "Ja";
    popupButton.style.margin = "0";
    popupButton.style.width = "calc(50% - 12px)";
    popupButton.style.height = "42px";
    popupButton.style.fontSize = "14px";
    popupButton.style.fontWeight = "600";
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

        // send the delete command to the server
        const data = {
            "command": "remove_last_event"
        }

        socket.send(JSON.stringify(data));
    });

    popupButtonContainer.appendChild(popupButton);

    const popupButton2 = document.createElement("button");
    popupButton2.classList.add("button");
    popupButton2.innerHTML = "Nee";
    popupButton2.style.margin = "0";
    popupButton2.style.width = "calc(50% - 12px)";
    popupButton2.style.height = "42px";
    popupButton2.style.fontSize = "14px";
    popupButton2.style.fontWeight = "600";
    popupButton2.style.background = "red";
    popupButton2.style.color = "var(--text-color)";
    popupButton2.style.border = "none";
    popupButton2.style.borderRadius = "4px";
    popupButton2.style.cursor = "pointer";
    popupButton2.style.userSelect = "none";

    popupButton2.addEventListener("click", function() {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();

        // remove the scroll lock
        document.body.style.overflow = "";
    });

    popupButtonContainer.appendChild(popupButton2);

    popup.appendChild(popupButtonContainer);

    // Append the popup to the overlay
    overlay.appendChild(popup);

    // Append the overlay to the body to cover the entire screen
    document.body.appendChild(overlay);

    // Disable scrolling on the body while the overlay is open
    document.body.style.overflow = "hidden";

    // add a event listener to the overlay so when clicked it closes the popup
    overlay.addEventListener("click", function() {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();

        // remove the scroll lock
        document.body.style.overflow = "";
    });

    // add a event listener to the popup so when clicked it doesn't close the overlay
    popup.addEventListener("click", function(event) {
        event.stopPropagation();
    });
}
