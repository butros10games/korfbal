let socket;
let team_id;
let WebSocket_url;
let infoContainer = document.getElementById("info-container");
let carousel = document.querySelector('.carousel');
let buttons = document.querySelectorAll('.button');

const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;
const maxLength = 14;

window.addEventListener("DOMContentLoaded", function() {
    const matches = regex.exec(url);
    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/club/" + team_id + "/";
    socket = initializeSocket(WebSocket_url, onMessageReceived);

    socket.onopen = function() {
        console.log("WebSocket connection established, sending initial data...");
        requestInitalData(".button.active", socket);
    };

    setupCarousel(carousel, buttons);
    setupFollowButton();
});

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    console.log(data);

    switch(data.command) {
        case "wedstrijden":
            cleanDom(infoContainer);

            updateMatches(data);
            break;
        
        case "teams":
            cleanDom(infoContainer);

            updateTeam(data);
            break;
    }
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
            home_team_logo.style.objectFit = "contain";
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
            
            if (element.status === 'finished') {
                const match_score = document.createElement("p");
                match_score.style.margin = "0";
                match_score.style.marginBottom = "12px";
                match_score.style.fontWeight = "600";
                match_score.innerHTML = element.home_score + " - " + element.away_score;

                match_date_container.appendChild(match_score);
            } else if (element.status === 'active') {
                const match_hour = document.createElement("p");
                match_hour.style.margin = "0";
                match_hour.style.marginBottom = "12px";
                match_hour.style.fontWeight = "600";
                match_hour.style.fontSize = "18px";
                match_hour.style.textAlign = "center";
                match_hour.innerHTML = element.start_time + "</br>" + " (live)";

                match_date_container.appendChild(match_hour);
            } else {
                const match_hour = document.createElement("p");
                match_hour.style.margin = "0";
                match_hour.style.marginBottom = "12px";
                match_hour.style.fontWeight = "600";
                match_hour.innerHTML = element.start_time;

                match_date_container.appendChild(match_hour);
            }
            match_container.appendChild(match_date_container);


            const away_team_container = document.createElement("div");
            away_team_container.classList.add("flex-column");
            away_team_container.style.width = "128px";

            const away_team_logo = document.createElement("img");
            away_team_logo.style.objectFit = "contain";
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
            team_picture.style.objectFit = "contain";
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