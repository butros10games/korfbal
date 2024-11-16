"use strict";

import { setupCarousel, updateMatches, updatePlayers, updateStatistics } from "../common/carousel/index.js";
import { initializeSocket, requestInitalData } from "../common/websockets/index.js";
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

    const WebSocket_url = "wss://" + window.location.host + "/ws/teams/" + team_id + "/";
    const socket = initializeSocket(WebSocket_url, (event) => {
        onMessageReceived(event, socket, user_id);
    });

    if (socket) {
        socket.onopen = function () {
            console.log("WebSocket connection established, sending initial data...");
            requestInitalData(".button.active", socket, { 'user_id': user_id });
        };
    } else {
        console.error("Failed to initialize WebSocket connection.");
    }

    setupCarousel(carousel, buttons, socket, { 'user_id': user_id }, 'get_stats');
    setupFollowButton(user_id, socket);
});

function onMessageReceived(event, socket, user_id) {
    const infoContainer = document.getElementById("info-container");
    const maxLength = 14;
    const data = JSON.parse(event.data);
    console.log(data);

    switch (data.command) {
        case "wedstrijden": {
            updateMatches(data, maxLength, infoContainer); // imported from common/updateMatches.js
            break;
        }

        case "stats": {
            updateStatistics(data.data, infoContainer, socket, user_id); // imported from common/updateStatistics.js
            break;
        }

        case "spelers": {
            updatePlayers(data, infoContainer);
            break;
        }
    }
}

