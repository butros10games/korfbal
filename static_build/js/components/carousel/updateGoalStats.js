export const updateGoalStats = function (data, infoContainer) {
    if (data.played_matches > 0) {
        const goals_container = document.createElement('div');
        goals_container.classList.add('flex-column');
        goals_container.style.width = 'calc(100% - 24px)';
        goals_container.style.padding = '12px';

        const row_1 = document.createElement('div');
        row_1.classList.add('flex-row');
        row_1.style.justifyContent = 'space-around';
        row_1.style.width = '100%';
        row_1.style.marginBottom = '24px';

        const matches_container = document.createElement('div');
        matches_container.classList.add('flex-column');
        matches_container.style.width = '144px';

        const matches = document.createElement('p');
        matches.style.margin = '0';
        matches.style.fontSize = '16px';
        matches.innerHTML = 'Wedstrijden';

        const matches_data = document.createElement('p');
        matches_data.style.margin = '0';
        matches_data.innerHTML = data.played_matches;

        matches_container.appendChild(matches);
        matches_container.appendChild(matches_data);

        row_1.appendChild(matches_container);

        const total_score_container = document.createElement('div');
        total_score_container.classList.add('flex-column');
        total_score_container.style.width = '144px';

        const total_score = document.createElement('p');
        total_score.style.margin = '0';
        total_score.style.fontSize = '16px';
        total_score.innerHTML = 'Totaal punten';

        const total_score_data = document.createElement('p');
        total_score_data.style.margin = '0';
        total_score_data.innerHTML =
            data.total_goals_for + '/' + data.total_goals_against;

        total_score_container.appendChild(total_score);
        total_score_container.appendChild(total_score_data);

        row_1.appendChild(total_score_container);

        goals_container.appendChild(row_1);

        // Create a container for goal stats per type
        const goal_stats_container = document.createElement('div');
        goal_stats_container.classList.add('flex-row');
        goal_stats_container.style.width = '100%';
        goal_stats_container.style.marginTop = '12px';
        goal_stats_container.style.flexWrap = 'wrap';
        goal_stats_container.style.justifyContent = 'space-around';

        // Iterate through goal_stats object
        for (const goalType in data.player_goal_stats) {
            if (Object.hasOwn(data.player_goal_stats, goalType)) {
                const goalStat = data.player_goal_stats[goalType];

                // Create a div for each goal type's stats
                const goal_type_container = document.createElement('div');
                goal_type_container.classList.add('flex-column');
                goal_type_container.style.marginBottom = '12px';
                goal_type_container.style.width = '104px';
                goal_type_container.style.marginBottom = '12px';

                const goal_type_name = document.createElement('p');
                goal_type_name.style.margin = '0';
                goal_type_name.style.fontSize = '16px';
                goal_type_name.innerHTML = goalType;

                const goals_data = document.createElement('p');
                goals_data.style.margin = '0';
                goals_data.innerHTML =
                    goalStat.goals_by_player + '/' + goalStat.goals_against_player;

                goal_type_container.appendChild(goal_type_name);
                goal_type_container.appendChild(goals_data);

                goal_stats_container.appendChild(goal_type_container);
            }
        }

        goals_container.appendChild(goal_stats_container);
        infoContainer.appendChild(goals_container);
    } else {
        infoContainer.classList.add('flex-center');
        infoContainer.innerHTML =
            "<p style='text-align: center;'>Er zijn nog geen doelpunten gemaakt</p>";
    }
};
