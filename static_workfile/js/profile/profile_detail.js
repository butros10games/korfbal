import { setupCarousel, updateMatches, updateTeam, updateSettings, updateGoalStats } from "../common/carousel";
import { setupProfilePicture } from "../common/profile_picture";
import { initializeSocket, requestInitalData } from "../common/websockets";

window.addEventListener("DOMContentLoaded", function() {
    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');
    const csrfToken = document.querySelector('input[name="csrfmiddlewaretoken"]').value;

    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = window.location.href;
    
    const matches = regex.exec(url);

    let player_id;

    if (matches) {
        player_id = matches[1];
        console.log(player_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    const WebSocket_url = "wss://" + window.location.host + "/ws/profile/" + player_id + "/";
    const socket = initializeSocket(WebSocket_url, (event) =>
        onMessageReceived(event, socket)
    );

    if (socket) {
        socket.onopen = function() {
        console.log("WebSocket connection established, sending initial data...");
        requestInitalData(".button.active", socket);
    };
        setupCarousel(carousel, buttons, socket);
    }
    setupCarousel(carousel, buttons, socket);
    setupProfilePicture(csrfToken);
});

function onMessageReceived(event, socket) {
    const infoContainer = document.getElementById("info-container");
    const profilePicture = document.getElementById("profilePic-container");
    const maxLength = 14;
    const data = JSON.parse(event.data);
    console.log(data);

    switch(data.command) {
        case "settings_request": {
            cleanDom(infoContainer, profilePicture);

            updateSettings(data, infoContainer, socket);
            break;
        }
        
        case "player_goal_stats": {
            cleanDom(infoContainer, profilePicture);

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
            cleanDom(infoContainer, profilePicture);

            updateTeam(data, infoContainer); // imported from common/updateTeam.js
            break;
        }

        case "matches": {
            cleanDom(infoContainer, profilePicture);

            updateMatches(data, maxLength, infoContainer); // imported from common/updateMatches.js
            break;
        }
    }
}

function cleanDom(element, profilePicture) {
    element.innerHTML = "";
    element.classList.remove("flex-center");
    element.classList.remove("flex-start-wrap");

    profilePicture.classList.remove("active-img");
}
