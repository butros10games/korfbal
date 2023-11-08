let socket;
let club_id;
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

const maxLength = 14;

window.addEventListener("DOMContentLoaded", function() {
    buttonWidth = document.querySelector('.button').offsetWidth;
    carousel = document.querySelector('.carousel');

    user_id = document.getElementById("user_id").innerText;
    infoContainer = document.getElementById("info-container");
    
    const matches = url.match(regex);

    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/club/" + team_id + "/";

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

function setNavButtons() {
    // Button selection for the carousel
    const buttons = document.querySelectorAll(".button");
    buttons.forEach((button) => {
        button.addEventListener("click", function () {
            // Deactivate the other buttons
            buttons.forEach((element) => {
                element.classList.remove("active");
            });

            this.classList.add("active");

            // Get data out of the button
            const data = this.getAttribute("data");

            socket.send(JSON.stringify({
                'command': data
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

    switch(data.command) {
        case "wedstrijden":
            cleanDom();

            updateMatches(data);
            break;
        
        case "teams":
            cleanDom();

            updateTeam(data);
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
            
            const match_hour = document.createElement("p");
            match_hour.style.margin = "0";
            match_hour.style.marginBottom = "12px";
            match_hour.style.fontWeight = "600";
            match_hour.innerHTML = element.start_time;

            match_date_container.appendChild(match_hour);
            match_container.appendChild(match_date_container);


            const away_team_container = document.createElement("div");
            away_team_container.classList.add("flex-column");
            away_team_container.style.width = "128px";

            const away_team_logo = document.createElement("img");
            away_team_logo.src = element.away_team_logo;
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
    var charsToShow = maxLength - 3;
    var frontChars = Math.ceil(charsToShow / 2);
    var backChars = Math.floor(charsToShow / 2);
  
    return text.substr(0, frontChars) + '...' + text.substr(text.length - backChars);
}

function updateTeam(data) {
    if (data.teams.length > 0) {
        for (const element of data.teams) {
            const team_container = document.createElement("a");
            team_container.classList.add("flex-row");
            team_container.style.justifyContent = "flex-start";
            team_container.style.padding = "12px";
            team_container.style.borderBottom = "1px solid rgb(0 0 0 / 20%)";
            team_container.style.width = "calc(100% - 24px)";
            team_container.style.textDecoration = "none";
            team_container.style.color = "#000";
            team_container.href = element.get_absolute_url;

            const team_picture = document.createElement("img");
            team_picture.src = element.logo;
            team_picture.style.width = "48px";
            team_picture.style.height = "48px";

            const team_name = document.createElement("p");
            team_name.style.margin = "12px 6px";
            team_name.style.fontSize = "14px";
            team_name.innerHTML = element.name;

            const arrow_div = document.createElement("div");
            arrow_div.classList.add("flex-center");
            arrow_div.style.width = "24px";
            arrow_div.style.height = "24px";
            arrow_div.style.marginLeft = "auto";

            const arrow = document.createElement("img");
            arrow.src = "/static/images/arrow.svg";
            arrow.style.width = "18px";
            // rotated arrow 90 degrees
            arrow.style.transform = "rotate(-90deg)";

            arrow_div.appendChild(arrow);

            team_container.appendChild(team_picture);
            team_container.appendChild(team_name);
            team_container.appendChild(arrow_div);

            infoContainer.appendChild(team_container);
        }
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen teams</p>";
    }
}