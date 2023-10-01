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
        
        case "goal_stats":
            cleanDom();

            updateGoalStats(data);
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