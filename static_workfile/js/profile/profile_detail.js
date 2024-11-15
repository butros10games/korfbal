"use strict";

import { setupCarousel, updateMatches, updateTeam, updateSettings, updateGoalStats } from "../common/carousel";
import { initializeSocket, requestInitalData } from "../common/websockets";

let socket;
let player_id;
let WebSocket_url;
let csrfToken;
const profilePicture = document.getElementById("profilePic-container");
const infoContainer = document.getElementById("info-container");
const carousel = document.querySelector('.carousel');
const buttons = document.querySelectorAll('.button');

const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;
const maxLength = 14;

window.addEventListener("DOMContentLoaded", function() {
    csrfToken = document.querySelector('input[name="csrfmiddlewaretoken"]').value;

    const matches = regex.exec(url);
    if (matches) {
        player_id = matches[1];
        console.log(player_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/profile/" + player_id + "/";
    socket = initializeSocket(WebSocket_url, onMessageReceived);

    socket.onopen = function() {
        console.log("WebSocket connection established, sending initial data...");
        requestInitalData(".button.active", socket);
    };

    setupCarousel(carousel, buttons, socket);
    setupProfilePicture();
});

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    console.log(data);

    switch(data.command) {
        case "settings_request": {
            cleanDom();

            updateSettings(data);
            break;
        }
        
        case "player_goal_stats": {
            cleanDom();

            updateGoalStats(data, infoContainer); // imported from common/updateGoalStats.js
            break;
        }

        case "settings_updated": {
            const saveButtonText = document.getElementById("save-button-text");
            saveButtonText.innerHTML = "Saved!";
            saveButtonText.style.color = "#fff";

            const saveButton = document.querySelector(".save-button");
            saveButton.classList.remove("loading");

            setTimeout(() => {
                saveButtonText.innerHTML = "Save";
                saveButtonText.style.color = "";
            }, 1500);
            break;
        }

        case "teams": {
            cleanDom();

            updateTeam(data, infoContainer); // imported from common/updateTeam.js
            break;
        }

        case "matches": {
            cleanDom();

            updateMatches(data, maxLength, infoContainer); // imported from common/updateMatches.js
            break;
        }
    }
}

function cleanDom() {
    infoContainer.innerHTML = "";
    infoContainer.classList.remove("flex-center");
    infoContainer.classList.remove("flex-start-wrap");

    profilePicture.classList.remove("active-img");
}
