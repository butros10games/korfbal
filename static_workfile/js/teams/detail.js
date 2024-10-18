// run when dom is loaded

let socket;
let team_id;
let WebSocket_url;
let infoContainer;
let user_id;
const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;

let touchStartX = 0;
let touchStartY = 0;
let touchEndX = 0;
let touchEndY = 0;
let isDragging = false;
let currentPosition = 0;
let startPosition = 0;

let buttonWidth;
let carousel;

const maxLength = 14;

window.addEventListener("DOMContentLoaded", function() {
    buttonWidth = document.querySelector('.button').offsetWidth;
    carousel = document.querySelector('.carousel');

    user_id = document.getElementById("user_id").innerText;
    infoContainer = document.getElementById("info-container");
    
    const matches = regex.match(url);

    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/teams/" + team_id + "/";

    load_icon();
    initializeSocket(WebSocket_url);
    setNavButtons();

    document.querySelector('.icon-container').addEventListener('click', function() {
        const isFollowed = this.getAttribute('data-followed') === 'true';
        
        // Toggle the data-followed attribute
        this.setAttribute('data-followed', !isFollowed);

        console.log("user_id: " + user_id);

        socket.send(JSON.stringify({
            'command': 'follow',
            'user_id': user_id,
            'followed': !isFollowed
        }));
    });
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
            socket.send(JSON.stringify({
                'command': data,
                'data_type': 'general'
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

    const infoContainer = document.getElementById("info-container");

    infoContainer.addEventListener('touchstart', (e) => {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
    });

    infoContainer.addEventListener('touchend', (e) => {
        touchEndX = e.changedTouches[0].clientX;
        touchEndY = e.changedTouches[0].clientY;
    
        const diffX = touchEndX - touchStartX;
        const diffY = touchEndY - touchStartY;
    
        // Check if it's a horizontal swipe
        if (Math.abs(diffX) > Math.abs(diffY)) {
            let activeIndex = Array.from(document.querySelectorAll(".button")).findIndex(button => button.classList.contains("active"));
    
            if (diffX > 30) { // Swipe right
                activeIndex = Math.max(activeIndex - 1, 0);
            } else if (diffX < -30) { // Swipe left
                activeIndex = Math.min(activeIndex + 1, buttons.length - 1);
            }
    
            changeActiveButton(activeIndex);
        }
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
            socket.send(JSON.stringify({
                'command': data,
                'data_type': 'general'
            }));
            cleanDom();
            load_icon();
        }
    });
}

function requestInitalData() {
    const button = document.querySelector(".button.active");
    const data = button.getAttribute('data');

    socket.send(JSON.stringify({
        'command': data,
        'data_type': 'general'
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
        case "wedstrijden":
            cleanDom();

            updateMatches(data);
            break;
        
        case "goal_stats":
            cleanDom();

            UpdateStatastics(data.data);
            break;

        case "spelers":
            cleanDom();
            
            updatePlayers(data);
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

function updateMatches(data) {
    if (data.wedstrijden.length > 0) {
        for (const element of data.wedstrijden) {
            const match_container = document.createElement("a");
            match_container.classList.add("match-container");
            match_container.classList.add("flex-row");
            match_container.style.justifyContent = "space-around";
            match_container.style.padding = "12px";
            match_container.style.borderBottom = "1px solid #000";
            match_container.style.width = "calc(100% - 24px)";
            match_container.style.textDecoration = "none";
            match_container.style.color = "#000";
            match_container.href = element.get_absolute_url;

            const homeTeamText = truncateMiddle(element.home_team, maxLength);
            const awayTeamText = truncateMiddle(element.away_team, maxLength);

            const home_team_container = document.createElement("div");
            home_team_container.classList.add("flex-column");
            home_team_container.style.width = "128px";

            const home_team_logo = document.createElement("img");
            home_team_logo.src = element.home_team_logo;
            home_team_logo.style.objectFit = "contain";
            home_team_logo.style.width = "64px";
            home_team_logo.style.height = "64px";

            const home_team_name = document.createElement("p");
            home_team_name.style.margin = "0";
            home_team_name.style.marginTop = "4px";
            home_team_name.style.fontSize = "12px";
            home_team_name.style.textAlign = "center";
            home_team_name.innerHTML = homeTeamText;

            home_team_container.appendChild(home_team_logo);
            home_team_container.appendChild(home_team_name);

            match_container.appendChild(home_team_container);

            const match_date_container = document.createElement("div");
            match_date_container.classList.add("flex-column");

            const match_date = document.createElement("p");
            match_date.style.margin = "0";
            match_date.style.marginBottom = "12px";
            match_date.innerHTML = element.start_date;

            match_date_container.appendChild(match_date);
            
            if (element.status === "finished") {
                const match_score = document.createElement("p");
                match_score.style.margin = "0";
                match_score.style.marginBottom = "12px";
                match_score.style.fontWeight = "600";
                match_score.innerHTML = element.home_score + " - " + element.away_score;

                match_date_container.appendChild(match_score);
            } else if (element.status === 'active') {
                const match_hour = document.createElement("p");
                match_hour.style.margin = "0";
                match_hour.style.marginBottom = "12px";
                match_hour.style.fontWeight = "600";
                match_hour.style.fontSize = "18px";
                match_hour.style.textAlign = "center";
                match_hour.innerHTML = element.start_time + "</br>" + " (live)";

                match_date_container.appendChild(match_hour);
            } else {
                const match_hour = document.createElement("p");
                match_hour.style.margin = "0";
                match_hour.style.marginBottom = "12px";
                match_hour.style.fontWeight = "600";
                match_hour.innerHTML = element.start_time;

                match_date_container.appendChild(match_hour);
            }
            match_container.appendChild(match_date_container);


            const away_team_container = document.createElement("div");
            away_team_container.classList.add("flex-column");
            away_team_container.style.width = "128px";

            const away_team_logo = document.createElement("img");
            away_team_logo.src = element.away_team_logo;
            away_team_logo.style.objectFit = "contain";
            away_team_logo.style.width = "64px";
            away_team_logo.style.height = "64px";

            const away_team_name = document.createElement("p");
            away_team_name.style.margin = "0";
            away_team_name.style.marginTop = "4px";
            away_team_name.style.fontSize = "12px";
            away_team_name.style.textAlign = "center";
            away_team_name.innerHTML = awayTeamText;

            away_team_container.appendChild(away_team_logo);
            away_team_container.appendChild(away_team_name);

            match_container.appendChild(away_team_container);

            infoContainer.appendChild(match_container);
        }
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen aankomende of gespeelde wedstrijden</p>";
    }
}

function truncateMiddle(text, maxLength) {
    if (text.length <= maxLength) {
        return text;
    }
  
    // Calculate the number of characters to show before and after the ellipsis
    const charsToShow = maxLength - 3;
    const frontChars = Math.ceil(charsToShow / 2);
    const backChars = Math.floor(charsToShow / 2);
  
    return text.substr(0, frontChars) + '...' + text.substr(text.length - backChars);
}

function UpdateStatastics(data) {
    const stats = data.stats;

    const statsContainer = document.createElement("div");
    statsContainer.classList.add("stats-container");

    if (stats) {
        // Check if the buttons already exist and if they exist, skip the creation of the buttons
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

        // Check if there is already a dataField and if there is a field delete it
        if (document.getElementById("dataField")) {
            document.getElementById("dataField").remove();
        }

        console.log(data);

        if (data.type == "general") {
            const goals_container = document.createElement("div");
            goals_container.classList.add("flex-column");
            goals_container.id = "dataField";
            goals_container.style.width = "calc(100% - 24px)";
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
                    goal_type_container.style.marginBottom = "12px";
                    goal_type_container.style.width = "104px";

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

            // Create a legend for the player stats
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
                playerName.innerHTML = truncateMiddle(player.username, 20);
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

function updatePlayers(data) {
    if (data.spelers.length > 0) {
        infoContainer.classList.add("flex-start-wrap");
        
        for (const element of data.spelers) {
            const player_container = document.createElement("a");
            player_container.href = element.get_absolute_url;
            player_container.style.textDecoration = "none";
            player_container.style.color = "#000";
            player_container.classList.add("player-container");

            const player_profile_pic = document.createElement("img");
            player_profile_pic.classList.add("player-profile-pic");
            player_profile_pic.src = element.profile_picture;
            player_profile_pic.style.objectFit = "cover";

            player_container.appendChild(player_profile_pic);

            const player_name = document.createElement("p");
            player_name.classList.add("player-name");
            player_name.style.fontSize = "14px";

            const PlayerName = truncateMiddle(element.name, 22);

            player_name.innerHTML = PlayerName;

            player_container.appendChild(player_name);

            infoContainer.appendChild(player_container);
        }
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen spelers toegevoegd</p>";
    }
}