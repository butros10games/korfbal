import { truncateMiddle } from "../utils";
import { cleanDomCarousel } from "./utils";

export const updateStatistics = function(data, infoContainer, socket, user_id) {
    const stats = data.stats;

    const statsContainer = document.createElement("div");
    statsContainer.classList.add("stats-container");

    if (stats) {
        // check if the buttons already exist and if they exist skip the creation of the buttons
        if (!document.querySelector(".stat-selector-button")) {
            cleanDomCarousel(infoContainer);

            const statSelectorButtonField = document.createElement("div");
            statSelectorButtonField.classList.add("flex-row");
            statSelectorButtonField.style.justifyContent = "space-around";
            statSelectorButtonField.style.margin = "12px";
            statSelectorButtonField.style.width = "calc(100% - 24px)";
            
            const buttonTypes = [
                { name: 'generaal', type: 'general' },
                { name: 'verloop', type: 'progression' },
                { name: 'spelers', type: 'player_stats' }
            ];

            buttonTypes.forEach(type => {
                const button = document.createElement("button");
                button.classList.add("stat-selector-button");

                // add to the first button a active class
                if (type.type === 'general') {
                    button.classList.add("active");
                }

                button.innerHTML = type.name;
                button.addEventListener('click', function() {
                    socket.send(JSON.stringify({
                        'command': 'get_stats',
                        'user_id': user_id,
                        'data_type': type.type
                    }));

                    // add active class to the button and remove it by the other buttons
                    const buttons = document.querySelectorAll(".stat-selector-button");
                    buttons.forEach((button_selector) => {
                        button_selector.classList.remove("active");
                    });
                    this.classList.add("active");
                });

                statSelectorButtonField.appendChild(button);
            });

            statsContainer.appendChild(statSelectorButtonField);
        }
        
        // check if there is already a dataField and if there is a field delete it
        if (document.getElementById("dataField")) {
            document.getElementById("dataField").remove();
        }

        console.log(data);

        if (data.type === "general") {
            general(stats, statsContainer);
        } else if (data.type === "player_stats") {
            playerStats(stats, statsContainer);
        }
    } else {
        const textElement = document.createElement("p");
        textElement.classList.add("flex-center");
        textElement.innerHTML = "<p>Geen statistieken gevonden.</p>";

        statsContainer.appendChild(textElement);
    }

    infoContainer.appendChild(statsContainer);
};

function general(stats, statsContainer) {
    const goals_container = document.createElement("div");
    goals_container.classList.add("flex-column");
    goals_container.id = "dataField";
    goals_container.style.width = "calc(100% - 24px))";
    goals_container.style.padding = "12px";

    const row_1 = document.createElement("div");
    row_1.classList.add("flex-row");
    row_1.style.justifyContent = "space-around";
    row_1.style.width = "100%";
    row_1.style.marginBottom = "24px";

    const total_score_container = document.createElement("div");
    total_score_container.classList.add("flex-column");
    total_score_container.style.width = "144px";

    const total_score = document.createElement("p");
    total_score.style.margin = "0";
    total_score.style.fontSize = "16px";
    total_score.innerHTML = "Totaal punten";

    const total_score_data = document.createElement("p");
    total_score_data.style.margin = "0";
    total_score_data.innerHTML = stats.goals_for + '/' + stats.goals_against;

    total_score_container.appendChild(total_score);
    total_score_container.appendChild(total_score_data);

    row_1.appendChild(total_score_container);

    goals_container.appendChild(row_1);

    // Create a container for goal stats per type
    const goal_stats_container = document.createElement("div");
    goal_stats_container.classList.add("flex-row");
    goal_stats_container.style.width = "100%";
    goal_stats_container.style.marginTop = "12px";
    goal_stats_container.style.flexWrap = "wrap";
    goal_stats_container.style.justifyContent = "space-around";

    // Iterate through goal_stats object
    for (const goalType of stats.goal_types) {
        console.log(goalType);
        if (stats.team_goal_stats.hasOwnProperty(goalType.name)) {
            const goalStat = stats.team_goal_stats[goalType.name];

            // Create a div for each goal type's stats
            const goal_type_container = document.createElement("div");
            goal_type_container.classList.add("flex-column");
            goal_type_container.style.marginbottom = "12px";
            goal_type_container.style.width = "104px";
            goal_type_container.style.marginBottom = "12px";

            const goal_type_name = document.createElement("p");
            goal_type_name.style.margin = "0";
            goal_type_name.style.fontSize = "16px";
            goal_type_name.innerHTML = goalType.name;

            const goals_data = document.createElement("p");
            goals_data.style.margin = "0";
            goals_data.innerHTML = goalStat.goals_by_player + "/" + goalStat.goals_against_player;

            goal_type_container.appendChild(goal_type_name);
            goal_type_container.appendChild(goals_data);

            goal_stats_container.appendChild(goal_type_container);
        }
    }

    goals_container.appendChild(goal_stats_container);
    statsContainer.appendChild(goals_container);
}

function playerStats(stats, statsContainer) {
    // Creating the player selector field
    const playerSelectorField = document.createElement("div");
    playerSelectorField.classList.add("flex-column");
    playerSelectorField.id = "dataField";
    playerSelectorField.style.margin = "24px 12px 0 12px";
    playerSelectorField.style.width = "calc(100% - 24px)";

    // create a lagenda for the player stats
    const legend = document.createElement("div");
    legend.classList.add("flex-row");
    legend.style.justifyContent = "space-between";
    legend.style.marginBottom = "12px";
    legend.style.borderBottom = "1px solid #ccc";
    legend.style.paddingBottom = "12px";
    
    const name = document.createElement("p");
    name.innerHTML = "Naam";
    name.style.margin = "0";
    name.style.fontSize = "16px";

    const score = document.createElement("p");
    score.classList.add("flex-center");
    score.innerHTML = "Score";
    score.style.width = "80px";
    score.style.margin = "0";
    score.style.fontSize = "16px";
    score.style.marginLeft = "auto";
    score.style.marginRight = "12px";

    const shots = document.createElement("p");
    shots.classList.add("flex-center");
    shots.innerHTML = "Schoten";
    shots.style.width = "80px";
    shots.style.margin = "0";
    shots.style.fontSize = "16px";

    legend.appendChild(name);
    legend.appendChild(score);
    legend.appendChild(shots);

    playerSelectorField.appendChild(legend);

    stats.player_stats.forEach(player => {
        const playerDataDiv = document.createElement("div");
        playerDataDiv.classList.add("flex-row");
        playerDataDiv.style.justifyContent = "space-between";
        playerDataDiv.style.marginBottom = "12px";
        playerDataDiv.style.borderBottom = "1px solid #ccc";
        playerDataDiv.style.paddingBottom = "12px";

        const playerName = document.createElement("p");
        playerName.innerHTML = truncateMiddle(player.username, 20);
        playerName.style.margin = "0";
        playerName.style.fontSize = "16px";

        const playerScore = document.createElement("p");
        playerScore.classList.add("flex-center");
        playerScore.innerHTML = player.goals_for + " / " + player.goals_against;
        playerScore.style.width = "80px";
        playerScore.style.margin = "0";
        playerScore.style.fontSize = "16px";
        playerScore.style.marginLeft = "auto";
        playerScore.style.marginRight = "12px";

        const playerShots = document.createElement("p");
        playerShots.classList.add("flex-center");
        playerShots.innerHTML = player.shots_for + " / " + player.shots_against;
        playerShots.style.width = "80px";
        playerShots.style.margin = "0";
        playerShots.style.fontSize = "16px";

        playerDataDiv.appendChild(playerName);
        playerDataDiv.appendChild(playerScore);
        playerDataDiv.appendChild(playerShots);

        playerSelectorField.appendChild(playerDataDiv);
    });

    statsContainer.appendChild(playerSelectorField);
}