// run when dom is loaded

let socket;
let team_id;
let WebSocket_url;
let infoContainer;

window.addEventListener("DOMContentLoaded", function() {
    infoContainer = document.getElementById("info-container");
    team_id = document.getElementById("team-id").innerHTML;

    console.log(team_id);

    WebSocket_url = "wss://" + window.location.host + "/ws/teams/" + team_id + "/";

    initializeSocket(WebSocket_url);
    setNavButtons();
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
            match_container.href = "/wedstrijden/" + data.wedstrijden[i].id + "/";

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
    if (data.goal_stats.length > 0) {
        console.log(data.goal_stats);
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen doelpunten gemaakt</p>";
    }
}

function updatePlayers(data) {
    if (data.spelers.length > 0) {
        for (i = 0; i < data.spelers.length; i++) {
            player_container = document.createElement("div");
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