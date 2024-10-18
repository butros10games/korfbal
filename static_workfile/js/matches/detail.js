let socket;
let match_id;
let user_id;
let WebSocket_url;
let infoContainer = document.getElementById("info-container");
let carousel = document.querySelector('.carousel');
let buttons = document.querySelectorAll('.button');

const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;
const maxLength = 14;

let timer = null;

window.addEventListener("DOMContentLoaded", function() {
    user_id = document.getElementById("user_id").innerText;

    const matches = regex.exec(url);
    if (matches) {
        match_id = matches[1];
        console.log(match_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/match/" + match_id + "/";
    socket = initializeSocket(WebSocket_url, onMessageReceived);

    socket.onopen = function() {
        console.log("WebSocket connection established, sending initial data...");
        requestInitalData(".button.active", socket, { 'user_id': user_id });
        socket.send(JSON.stringify({'command': 'get_time'}));
    };

    setupCarousel(carousel, buttons, { 'user_id': user_id }, 'get_stats');
});

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    console.log(data);

    switch(data.command) {
        case "events": {
            cleanDom(infoContainer);

            updateEvents(data);
            break;
        }
        
        case "playerGroups": {
            cleanDom(infoContainer);

            if (data.is_coach && !data.status == 'finished') {
                updateplayerGroups(data);
            } else {
                showPlayerGroups(data);
            }
            break;
        }
        
        case "team_goal_change": {
            const scoreField = document.getElementById("score");
            scoreField.innerHTML = data.goals_for + " / " + data.goals_against;

            break;
        }

        case "stats": {
            UpdateStatastics(data.data); // imported from common/updateStatastics.js
            break;
        }

        case "timer_data": {
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
        }

        case "pause": {
            if (data.pause === true) {
                timer.stop();
                console.log("Timer paused");

            } else if (data.pause === false) {
                timer.start(data.pause_time);
                console.log("Timer resumed");
            }

            break;
        }
    }
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
                eventTypeDiv.style.width = "64px";

                if (event.for_team) {
                    eventTypeDiv.style.backgroundColor = '#4CAF50';
                    thuis++;
                } else {
                    eventTypeDiv.style.backgroundColor = 'rgba(235, 0, 0, 0.7)';
                    uit++;
                }

                const midsectionDiv = document.createElement("div");
                midsectionDiv.classList.add("flex-column");

                const descriptionDiv = document.createElement("div");
                descriptionDiv.classList.add("description");
                descriptionDiv.innerHTML = event.goal_type + " (\"" + event.time + "\")";

                const playerName = document.createElement("p");
                playerName.innerHTML = truncateMiddle(event.player, 20);
                playerName.style.margin = "0";

                midsectionDiv.appendChild(descriptionDiv);
                midsectionDiv.appendChild(playerName);

                const currentScoreDiv = document.createElement("div");
                currentScoreDiv.classList.add("current-score");
                currentScoreDiv.innerHTML = thuis + "-" + uit;
                currentScoreDiv.style.width = "64px";

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(currentScoreDiv);
            } else if (event.type == "wissel") {
                const eventTypeDiv = document.createElement("div");
                eventTypeDiv.classList.add("event-type");
                eventTypeDiv.innerHTML = event.type;
                eventTypeDiv.style.width = "64px";
                eventTypeDiv.style.backgroundColor = '#eb9834';

                const midsectionDiv = document.createElement("div");
                midsectionDiv.classList.add("flex-column");

                const descriptionDiv = document.createElement("div");
                descriptionDiv.classList.add("description");
                descriptionDiv.innerHTML = "(\"" + event.time + "\")";


                const playerName = document.createElement("p");
                playerName.innerHTML = truncateMiddle(event.player_in, 15) + " --> " + truncateMiddle(event.player_out, 15);
                playerName.style.margin = "0";
                playerName.style.fontSize = "12px";

                midsectionDiv.appendChild(descriptionDiv);
                midsectionDiv.appendChild(playerName);

                const endSectionDiv = document.createElement("div");
                endSectionDiv.style.width = "64px";

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(endSectionDiv);
            } else if (event.type == "pauze") {
                const eventTypeDiv = document.createElement("div");
                eventTypeDiv.classList.add("event-type");
                eventTypeDiv.innerHTML = event.type;
                eventTypeDiv.style.width = "64px";
                eventTypeDiv.style.backgroundColor = '#2196F3';

                const midsectionDiv = document.createElement("div");
                midsectionDiv.classList.add("flex-column");

                const descriptionDiv = document.createElement("div");
                descriptionDiv.classList.add("description");
                descriptionDiv.innerHTML = "(\"" + event.time + "\")";

                const timeout_div = document.createElement("p");
                timeout_div.style.margin = "0";
                timeout_div.style.fontSize = "14px";
                if (event.end_time == null) {
                    // Convert the start time to a date object and format it so only the hour and minutes are shown
                    const start_time = new Date(event.start_time);

                    timeout_div.innerHTML = start_time.getHours().toString().padStart(2, '0') + ":" + start_time.getMinutes().toString().padStart(2, '0')
                } else {
                    const start_time = new Date(event.start_time);
                    const end_time = new Date(event.end_time);

                    timeout_div.innerHTML = start_time.getHours().toString().padStart(2, '0') + ":" + start_time.getMinutes().toString().padStart(2, '0') + " - " + end_time.getHours().toString().padStart(2, '0') + ":" + end_time.getMinutes().toString().padStart(2, '0');
                }

                midsectionDiv.appendChild(descriptionDiv);
                midsectionDiv.appendChild(timeout_div);

                const endSectionDiv = document.createElement("div");
                endSectionDiv.style.width = "64px";

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(endSectionDiv);
            }

            eventContainer.appendChild(eventDiv);
        });
    } else {
        const textElement = document.createElement("p");
        textElement.classList.add("flex-center");
        textElement.innerHTML = "<p>Geen events gevonden.</p>";

        eventContainer.appendChild(textElement);
    }

    if (data.access && !data.status == 'finished') {
        const buttonContainer = document.createElement("div");
        buttonContainer.classList.add("flex-center");
        buttonContainer.style.marginTop = "12px";

        const trackerButton = document.createElement("a");
        trackerButton.classList.add("tracker-button");
        trackerButton.href = "/match/selector/" + match_id + "/";
        trackerButton.innerHTML = "bijhouden";
        trackerButton.style.marginBottom = "12px";

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
                    playerName.innerHTML = truncateMiddle(player.name, 16);
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
                option.innerHTML = truncateMiddle(dataPlayer.name, 18);
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
