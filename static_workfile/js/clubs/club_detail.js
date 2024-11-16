import { setupCarousel, updateMatches, updateTeam } from "../common/carousel";
import { initializeSocket, requestInitalData } from "../common/websockets";
import { setupFollowButton } from "../common/setup_follow_button.js";


window.addEventListener("DOMContentLoaded", function() {
    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');
    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = window.location.href;

    const user_id = document.getElementById("user_id").innerText;
    const matches = regex.exec(url);

    let team_id;

    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    const WebSocket_url = "wss://" + window.location.host + "/ws/club/" + team_id + "/";
    const socket = initializeSocket(WebSocket_url, onMessageReceived);

    if (socket) {
        socket.onopen = function() {
            console.log("WebSocket connection established, sending initial data...");
            requestInitalData(".button.active", socket);
        };
    } else {
        console.error("Failed to initialize WebSocket connection.");
    }

    setupCarousel(carousel, buttons, socket);
    setupFollowButton(user_id, socket);
});

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    const infoContainer = document.getElementById("info-container");
    const maxLength = 14;

    switch(data.command) {
        case "wedstrijden":
            updateMatches(data, maxLength, infoContainer); // imported from common/updateMatches.js
            break;
        
        case "teams":
            updateTeam(data, infoContainer); // imported from common/updateTeam.js
            break;
    }
}
