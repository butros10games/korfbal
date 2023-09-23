// run when dom is loaded

let socket;
let team_id;
let WebSocket_url;
let infoContainer;
const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;

window.addEventListener("DOMContentLoaded", function() {
    infoContainer = document.getElementById("info-container");
    
    const matches = url.match(regex);

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
    });
});

function setNavButtons() {
    // button selection for the team detail page
    buttons = document.querySelectorAll(".button");
    buttons.forEach(button => {
        button.addEventListener("click", function() {
            // deactivated the other buttons
            var otherButtons = document.querySelectorAll(".button");
            otherButtons.forEach(element => {
                element.classList.remove("active");
            });

            this.classList.toggle("active");

            // Get data out of the button
            var data = this.getAttribute('data');
            
            socket.send(JSON.stringify({
                'command': data
            }));

            load_icon();
        });
    });
}

function requestInitalData() {
    button = document.querySelector(".button.active");
    var data = button.getAttribute('data');

    socket.send(JSON.stringify({
        'command': data
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

    cleanDom();

    switch(data.command) {
        case "wedstrijden":
            updateMatches(data);
            break;
        
        case "goal_stats":
            updateGoalStats(data);
            break;

        case "spelers":
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
}

function updateMatches(data) {
    if (data.wedstrijden.length > 0) {
        for (i = 0; i < data.wedstrijden.length; i++) {
            match_container = document.createElement("a");
            match_container.classList.add("match-container");
            match_container.style.padding = "12px";
            match_container.style.borderBottom = "1px solid #000";
            match_container.style.width = "calc(100% - 24px)";
            match_container.style.display = "block";
            match_container.style.textDecoration = "none";
            match_container.style.color = "#000";
            match_container.href = data.wedstrijden[i].get_absolute_url;

            match_date_container = document.createElement("div");
            match_date_container.classList.add("flex-row");

            match_date = document.createElement("p");
            match_date.style.margin = "0";
            match_date.style.marginBottom = "12px";
            match_date.innerHTML = data.wedstrijden[i].start_date;

            match_date_container.appendChild(match_date);
            
            match_hour = document.createElement("p");
            match_hour.style.margin = "0";
            match_hour.style.marginBottom = "12px";
            match_hour.innerHTML = data.wedstrijden[i].start_time;

            match_date_container.appendChild(match_hour);
            match_container.appendChild(match_date_container);

            home_team_container = document.createElement("div");
            home_team_container.classList.add("flex-row");

            home_team_name = document.createElement("p");
            home_team_name.style.margin = "0";
            home_team_name.innerHTML = data.wedstrijden[i].home_team;

            home_team_score = document.createElement("p");
            home_team_score.style.margin = "0";
            home_team_score.innerHTML = data.wedstrijden[i].home_score;

            home_team_container.appendChild(home_team_name);
            home_team_container.appendChild(home_team_score);

            match_container.appendChild(home_team_container);

            away_team_container = document.createElement("div");
            away_team_container.classList.add("flex-row");

            away_team_name = document.createElement("p");
            away_team_name.style.margin = "0";
            away_team_name.innerHTML = data.wedstrijden[i].away_team;

            away_team_score = document.createElement("p");
            away_team_score.style.margin = "0";
            away_team_score.innerHTML = data.wedstrijden[i].away_score;

            away_team_container.appendChild(away_team_name);
            away_team_container.appendChild(away_team_score);

            match_container.appendChild(away_team_container);

            infoContainer.appendChild(match_container);
        }
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen aankomende of gespeelde wedstrijden</p>";
    }
}

function updateGoalStats(data) {
    if (data.played_matches > 0) {
        goals_container = document.createElement("div");
        goals_container.classList.add("flex-column");
        goals_container.style.width = "calc(100% - 24px))";
        goals_container.style.padding = "12px";

        row_1 = document.createElement("div");
        row_1.classList.add("flex-row");
        row_1.style.justifyContent = "space-around";
        row_1.style.width = "100%";
        row_1.style.marginBottom = "24px";
        
        matchs_container = document.createElement("div");
        matchs_container.classList.add("flex-column");
        matchs_container.style.width = "144px";

        matchs = document.createElement("p");
        matchs.style.margin = "0";
        matchs.style.fontSize = "14px";
        matchs.innerHTML = "Wedstrijden";

        matchs_data = document.createElement("p");
        matchs_data.style.margin = "0";
        matchs_data.innerHTML = data.played_matches;

        matchs_container.appendChild(matchs);
        matchs_container.appendChild(matchs_data);

        row_1.appendChild(matchs_container);

        total_score_container = document.createElement("div");
        total_score_container.classList.add("flex-column");
        total_score_container.style.width = "144px";

        total_score = document.createElement("p");
        total_score.style.margin = "0";
        total_score.style.fontSize = "14px";
        total_score.innerHTML = "Totaal punten";

        total_score_data = document.createElement("p");
        total_score_data.style.margin = "0";
        total_score_data.innerHTML = data.total_goals_for + '/' + data.total_goals_against;

        total_score_container.appendChild(total_score);
        total_score_container.appendChild(total_score_data);

        row_1.appendChild(total_score_container);

        goals_container.appendChild(row_1);

        row_2 = document.createElement("div");

        // Create a container for goal stats per type
        goal_stats_container = document.createElement("div");
        goal_stats_container.classList.add("flex-row");
        goal_stats_container.style.width = "100%";
        goal_stats_container.style.marginTop = "12px";
        goal_stats_container.style.flexWrap = "wrap";
        goal_stats_container.style.justifyContent = "space-around";

        // Iterate through goal_stats object
        for (const goalType in data.goal_stats) {
            if (data.goal_stats.hasOwnProperty(goalType)) {
                const goalStat = data.goal_stats[goalType];

                // Create a div for each goal type's stats
                goal_type_container = document.createElement("div");
                goal_type_container.classList.add("flex-column");
                goal_type_container.style.marginbottom = "12px";
                goal_type_container.style.width = "104px";
                goal_type_container.style.marginBottom = "12px";

                goal_type_name = document.createElement("p");
                goal_type_name.style.margin = "0";
                goal_type_name.style.fontSize = "14px";
                goal_type_name.innerHTML = goalType;

                goals_data = document.createElement("p");
                goals_data.style.margin = "0";
                goals_data.innerHTML = goalStat.goals_for + "/" + goalStat.goals_against;

                goal_type_container.appendChild(goal_type_name);
                goal_type_container.appendChild(goals_data);

                goal_stats_container.appendChild(goal_type_container);
            }
        }

        goals_container.appendChild(goal_stats_container);
        infoContainer.appendChild(goals_container);
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen doelpunten gemaakt</p>";
    }
}

function updatePlayers(data) {
    if (data.spelers.length > 0) {
        for (i = 0; i < data.spelers.length; i++) {
            player_container = document.createElement("a");
            player_container.href = data.spelers[i].get_absolute_url;
            player_container.style.textDecoration = "none";
            player_container.style.color = "#000";
            player_container.classList.add("player-container");

            player_profile_pic = document.createElement("img");
            player_profile_pic.classList.add("player-profile-pic");
            player_profile_pic.src = data.spelers[i].profile_picture;

            player_container.appendChild(player_profile_pic);

            player_name = document.createElement("p");
            player_name.classList.add("player-name");
            player_name.innerHTML = data.spelers[i].name;

            player_container.appendChild(player_name);

            infoContainer.appendChild(player_container);
        }
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen spelers toegevoegd</p>";
    }
}