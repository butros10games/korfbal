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

            if (data.is_coach && !data.finished) {
                updateplayerGroups(data); // imported from matches/common/updateplayerGroups.js
            } else {
                showPlayerGroups(data); // imported from matches/common/showPlayerGroups.js
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
    let thuis = 0, uit = 0;

    const eventContainer = document.createElement("div");
    eventContainer.classList.add("event-container");

    if (events.length > 0) {
        events.forEach(event => {
            const eventDiv = document.createElement("div");
            eventDiv.classList.add("event", "flex-row");

            if (event.type == "goal") {
                const eventTypeDiv = createEventTypeDiv(event.type, "64px", event.for_team ? '#4CAF50' : 'rgba(235, 0, 0, 0.7)');
                if (event.for_team) thuis++; else uit++;
                const midsectionDiv = createMidsectionDiv(event.goal_type + " (\"" + event.time + "\")", truncateMiddle(event.player, 20));
                const scoreDiv = createScoreDiv(thuis + "-" + uit, "64px");

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(scoreDiv);
            } else if (event.type == "wissel") {
                const eventTypeDiv = createEventTypeDiv(event.type, "64px", '#eb9834');
                const midsectionDiv = createMidsectionDiv("(\"" + event.time + "\")", truncateMiddle(event.player_in, 15) + " --> " + truncateMiddle(event.player_out, 15));

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
            } else if (event.type == "pauze") {
                const eventTypeDiv = createEventTypeDiv(event.type, "64px", '#2196F3');
                const midsectionDiv = createMidsectionDiv("(\"" + event.time + "\")", getFormattedTime(event));

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
            }

            eventContainer.appendChild(eventDiv);
        });
    } else {
        const textElement = document.createElement("p");
        textElement.classList.add("flex-center");
        textElement.innerHTML = "<p>Geen events gevonden.</p>";
        eventContainer.appendChild(textElement);
    }

    console.log('test')

    // Adding the tracker button if required
    if (data.access && data.status != 'finished') {
        console.log('active')
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
