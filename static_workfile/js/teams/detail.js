// run when dom is loaded
let socket;
let team_id;
let user_id;
let WebSocket_url;
const infoContainer = document.getElementById("info-container");
const carousel = document.querySelector('.carousel');
const buttons = document.querySelectorAll('.button');

const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;
const maxLength = 14;

window.addEventListener("DOMContentLoaded", function() {
    user_id = document.getElementById("user_id").innerText;

    const matches = regex.exec(url);
    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/teams/" + team_id + "/";
    socket = initializeSocket(WebSocket_url, onMessageReceived);

    socket.onopen = function() {
        console.log("WebSocket connection established, sending initial data...");
        requestInitalData(".button.active", socket, { 'user_id': user_id });
    };

    setupCarousel(carousel, buttons, { 'user_id': user_id }, 'get_stats');
    setupFollowButton(user_id);
});

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    console.log(data);

    switch(data.command) {
        case "wedstrijden": {
            cleanDom(infoContainer);

            updateMatches(data); // imported from common/updateMatches.js
            break;
        }
        
        case "stats": {
            UpdateStatastics(data.data); // imported from common/UpdateStatastics.js
            break;
        }

        case "spelers": {
            cleanDom(infoContainer);
            
            updatePlayers(data);
            break;
        }
    }
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
            player_name.style.fontSize = "16px";

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