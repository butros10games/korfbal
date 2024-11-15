"use strict";

export const updateMatches = function(data, maxLength, infoContainer) {
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
            home_team_name.style.fontSize = "14px";
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
                match_hour.style.fontSize = "20px";
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
            away_team_name.style.fontSize = "14px";
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
};