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

let isAutoScrolling = false; // Flag to track if we are auto-scrolling

function setNavButtons() {
    // Button selection for the carousel
    const buttons = document.querySelectorAll(".button");

    buttons.forEach((button) => {
        button.addEventListener("click", function () {
            if (isAutoScrolling) return;

            buttons.forEach(element => element.classList.remove("active"));
            this.classList.add("active");

            isAutoScrolling = true;
            this.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
            setTimeout(() => isAutoScrolling = false, 500);

            const data = this.getAttribute("data");
            socket.send(JSON.stringify({ 'command': data }));
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

    const infoContainer = document.getElementById("info-container");

    infoContainer.addEventListener('touchstart', (e) => {
        touchStartX = e.touches[0].clientX;
    });

    infoContainer.addEventListener('touchend', (e) => {
        touchEndX = e.changedTouches[0].clientX;
        const diff = touchEndX - touchStartX;

        let activeIndex = Array.from(document.querySelectorAll(".button")).findIndex(button => button.classList.contains("active"));

        if (diff > 30) { // Swipe right
            activeIndex = Math.max(activeIndex - 1, 0);
        } else if (diff < -30) { // Swipe left
            activeIndex = Math.min(activeIndex + 1, buttons.length - 1);
        }

        changeActiveButton(activeIndex);
    });
}

function changeActiveButton(newActiveIndex) {
    const buttons = document.querySelectorAll(".button");

    buttons.forEach((button, index) => {
        button.classList.remove("active");
        if (index === newActiveIndex) {
            button.classList.add("active");

            if (!isAutoScrolling) {
                isAutoScrolling = true;
                button.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
                setTimeout(() => isAutoScrolling = false, 500);
            }

            const data = button.getAttribute("data");
            socket.send(JSON.stringify({ 'command': data }));
            cleanDom();
            load_icon();
        }
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

            if (data.is_coach && !data.finished) {
                updateplayerGroups(data);
            } else {
                showPlayerGroups(data);
            }
            break;

        case "stats":
            UpdateStatastics(data.data);
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
                playerName.innerHTML = event.player;
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
                playerName.innerHTML = event.player_in + " --> " + event.player_out;
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
                    start_time = new Date(event.start_time);

                    timeout_div.innerHTML = start_time.getHours().toString().padStart(2, '0') + ":" + start_time.getMinutes().toString().padStart(2, '0')
                } else {
                    start_time = new Date(event.start_time);
                    end_time = new Date(event.end_time);

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

    if (data.access && !data.finished) {
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

function UpdateStatastics(data) {
    const stats = data.stats;

    const statsContainer = document.createElement("div");
    statsContainer.classList.add("stats-container");

    if (stats) {
        // check if the buttons already exist and if they exist skip the creation of the buttons
        if (!document.querySelector(".stat-selector-button")) {
            cleanDom();

            const statSelectorButtonField = document.createElement("div");
            statSelectorButtonField.classList.add("flex-row");
            statSelectorButtonField.style.justifyContent = "space-around";
            statSelectorButtonField.style.margin = "12px";
            statSelectorButtonField.style.width = "calc(100% - 24px)";
            
            const buttonTypes = [
                { name: 'generaal', type: 'general' },
                { name: 'verloop', type: 'progression' },
                { name: 'spelers', type: 'player_stats' }
            ];

            buttonTypes.forEach(type => {
                const button = document.createElement("button");
                button.classList.add("stat-selector-button");

                // add to the first button a active class
                if (type.type == 'general') {
                    button.classList.add("active");
                }

                button.innerHTML = type.name;
                button.addEventListener('click', function() {
                    socket.send(JSON.stringify({
                        'command': 'get_stats',
                        'user_id': user_id,
                        'data_type': type.type
                    }));

                    // add active class to the button and remove it by the other buttons
                    const buttons = document.querySelectorAll(".stat-selector-button");
                    buttons.forEach((button) => {
                        button.classList.remove("active");
                    });
                    this.classList.add("active");
                });

                statSelectorButtonField.appendChild(button);
            });

            statsContainer.appendChild(statSelectorButtonField);
        }
        
        // check if there is already a dataField and if there is a field delete it
        if (document.getElementById("dataField")) {
            document.getElementById("dataField").remove();
        }

        console.log(data);

        if (data.type == "general") {
            const goals_container = document.createElement("div");
            goals_container.classList.add("flex-column");
            goals_container.id = "dataField";
            goals_container.style.width = "calc(100% - 24px))";
            goals_container.style.padding = "12px";

            const row_1 = document.createElement("div");
            row_1.classList.add("flex-row");
            row_1.style.justifyContent = "space-around";
            row_1.style.width = "100%";
            row_1.style.marginBottom = "24px";

            const total_score_container = document.createElement("div");
            total_score_container.classList.add("flex-column");
            total_score_container.style.width = "144px";

            const total_score = document.createElement("p");
            total_score.style.margin = "0";
            total_score.style.fontSize = "14px";
            total_score.innerHTML = "Totaal punten";

            const total_score_data = document.createElement("p");
            total_score_data.style.margin = "0";
            total_score_data.innerHTML = stats.goals_for + '/' + stats.goals_against;

            total_score_container.appendChild(total_score);
            total_score_container.appendChild(total_score_data);

            row_1.appendChild(total_score_container);

            goals_container.appendChild(row_1);

            // Create a container for goal stats per type
            const goal_stats_container = document.createElement("div");
            goal_stats_container.classList.add("flex-row");
            goal_stats_container.style.width = "100%";
            goal_stats_container.style.marginTop = "12px";
            goal_stats_container.style.flexWrap = "wrap";
            goal_stats_container.style.justifyContent = "space-around";

            // Iterate through goal_stats object
            for (const goalType of stats.goal_types) {
                console.log(goalType);
                if (stats.team_goal_stats.hasOwnProperty(goalType.name)) {
                    const goalStat = stats.team_goal_stats[goalType.name];

                    // Create a div for each goal type's stats
                    const goal_type_container = document.createElement("div");
                    goal_type_container.classList.add("flex-column");
                    goal_type_container.style.marginbottom = "12px";
                    goal_type_container.style.width = "104px";
                    goal_type_container.style.marginBottom = "12px";

                    const goal_type_name = document.createElement("p");
                    goal_type_name.style.margin = "0";
                    goal_type_name.style.fontSize = "14px";
                    goal_type_name.innerHTML = goalType.name;

                    const goals_data = document.createElement("p");
                    goals_data.style.margin = "0";
                    goals_data.innerHTML = goalStat.goals_by_player + "/" + goalStat.goals_against_player;

                    goal_type_container.appendChild(goal_type_name);
                    goal_type_container.appendChild(goals_data);

                    goal_stats_container.appendChild(goal_type_container);
                }
            }

            goals_container.appendChild(goal_stats_container);
            statsContainer.appendChild(goals_container);
        } else if (data.type == "player_stats") {
            // Creating the player selector field
            const playerSelectorField = document.createElement("div");
            playerSelectorField.classList.add("flex-column");
            playerSelectorField.id = "dataField";
            playerSelectorField.style.margin = "24px 12px 0 12px";
            playerSelectorField.style.width = "calc(100% - 24px)";

            // create a lagenda for the player stats
            const legend = document.createElement("div");
            legend.classList.add("flex-row");
            legend.style.justifyContent = "space-between";
            legend.style.marginBottom = "12px";
            legend.style.borderBottom = "1px solid #ccc";
            legend.style.paddingBottom = "12px";
            
            const name = document.createElement("p");
            name.innerHTML = "Naam";
            name.style.margin = "0";
            name.style.fontSize = "14px";

            const score = document.createElement("p");
            score.classList.add("flex-center");
            score.innerHTML = "Score";
            score.style.width = "80px";
            score.style.margin = "0";
            score.style.fontSize = "14px";
            score.style.marginLeft = "auto";
            score.style.marginRight = "12px";

            const shots = document.createElement("p");
            shots.classList.add("flex-center");
            shots.innerHTML = "Schoten";
            shots.style.width = "80px";
            shots.style.margin = "0";
            shots.style.fontSize = "14px";

            legend.appendChild(name);
            legend.appendChild(score);
            legend.appendChild(shots);

            playerSelectorField.appendChild(legend);

            stats.player_stats.forEach(player => {
                const playerDataDiv = document.createElement("div");
                playerDataDiv.classList.add("flex-row");
                playerDataDiv.style.justifyContent = "space-between";
                playerDataDiv.style.marginBottom = "12px";
                playerDataDiv.style.borderBottom = "1px solid #ccc";
                playerDataDiv.style.paddingBottom = "12px";

                const playerName = document.createElement("p");
                playerName.innerHTML = player.username;
                playerName.style.margin = "0";
                playerName.style.fontSize = "14px";

                const playerScore = document.createElement("p");
                playerScore.classList.add("flex-center");
                playerScore.innerHTML = player.goals_for + " / " + player.goals_against;
                playerScore.style.width = "80px";
                playerScore.style.margin = "0";
                playerScore.style.fontSize = "14px";
                playerScore.style.marginLeft = "auto";
                playerScore.style.marginRight = "12px";

                const playerShots = document.createElement("p");
                playerShots.classList.add("flex-center");
                playerShots.innerHTML = player.shots_for + " / " + player.shots_against;
                playerShots.style.width = "80px";
                playerShots.style.margin = "0";
                playerShots.style.fontSize = "14px";

                playerDataDiv.appendChild(playerName);
                playerDataDiv.appendChild(playerScore);
                playerDataDiv.appendChild(playerShots);

                playerSelectorField.appendChild(playerDataDiv);
            });

            statsContainer.appendChild(playerSelectorField);
        }
    } else {
        const textElement = document.createElement("p");
        textElement.classList.add("flex-center");
        textElement.innerHTML = "<p>Geen statistieken gevonden.</p>";

        statsContainer.appendChild(textElement);
    }

    infoContainer.appendChild(statsContainer);
}