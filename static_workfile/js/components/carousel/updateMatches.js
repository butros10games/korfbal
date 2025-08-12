import { truncateMiddle } from '../../utils/index.js';
import { cleanDomCarousel } from '../../utils/dom/index.js';

export const updateMatches = function (data, maxLength, infoContainer, socket) {
    cleanDomCarousel(infoContainer);
    let current_date = null;

    if (data.matches.length > 0) {
        for (const element of data.matches) {
            // Date container
            if (element.start_date !== current_date) {
                const date_container = document.createElement('div');
                date_container.classList.add('flex-center');
                date_container.style.width = '100%';
                date_container.style.padding = '6px 0';
                date_container.style.backgroundColor = 'var(--off-white)';
                date_container.style.borderBottom = '1px solid var(--gray)';

                const date = document.createElement('p');
                date.style.margin = '0';
                date.style.fontWeight = '600';
                date.style.color = 'var(--primary_color)';
                date.innerHTML = element.start_date;

                date_container.appendChild(date);
                infoContainer.appendChild(date_container);

                current_date = element.start_date;
            }

            const match_container = document.createElement('a');
            match_container.classList.add('match-container');
            match_container.classList.add('flex-row');
            match_container.style.justifyContent = 'space-around';
            match_container.style.padding = '12px';
            match_container.style.borderBottom = '1px solid var(--gray)';
            match_container.style.width = 'calc(100% - 24px)';
            match_container.style.textDecoration = 'none';
            match_container.style.color = '#000';
            match_container.href = element.get_absolute_url;

            const homeTeamText = truncateMiddle(element.home_team, maxLength);
            const awayTeamText = truncateMiddle(element.away_team, maxLength);

            const home_team_container = document.createElement('div');
            home_team_container.classList.add('flex-column');
            home_team_container.style.width = '128px';

            const home_team_logo = document.createElement('img');
            home_team_logo.style.objectFit = 'contain';
            home_team_logo.src = element.home_team_logo;
            home_team_logo.style.width = '64px';
            home_team_logo.style.height = '64px';

            const home_team_name = document.createElement('p');
            home_team_name.style.margin = '0';
            home_team_name.style.marginTop = '4px';
            home_team_name.style.fontSize = '14px';
            home_team_name.style.textAlign = 'center';
            home_team_name.innerHTML = homeTeamText;

            home_team_container.appendChild(home_team_logo);
            home_team_container.appendChild(home_team_name);

            match_container.appendChild(home_team_container);

            const match_middle_container = document.createElement('div');
            match_middle_container.classList.add('flex-column');
            match_middle_container.style.width = '80px';

            if (element.status === 'finished') {
                const match_score = document.createElement('p');
                match_score.style.margin = '0';
                match_score.style.marginBottom = '12px';
                match_score.style.fontSize = '22px';
                match_score.style.fontWeight = '600';
                match_score.innerHTML = element.home_score + ' - ' + element.away_score;

                match_middle_container.appendChild(match_score);
            } else if (element.status === 'active') {
                const match_period = document.createElement('p');
                match_period.style.fontSize = '14px';
                match_period.style.margin = '0';
                match_period.innerHTML = `Periode <span id="periode_number">${element.current_part}</span>/${element.parts}`;

                const match_counter = document.createElement('p');
                match_counter.id = 'counter_' + element.match_data_id;
                match_counter.style.margin = '0';
                match_counter.style.fontSize = '22px';
                match_counter.style.fontWeight = '600';
                match_counter.innerHTML = element.time_display;

                const match_score = document.createElement('p');
                match_score.id = 'score';
                match_score.style.margin = '0';
                match_score.innerHTML = element.home_score + ' - ' + element.away_score;

                match_middle_container.appendChild(match_period);
                match_middle_container.appendChild(match_counter);
                match_middle_container.appendChild(match_score);

                socket.send(
                    JSON.stringify({
                        command: 'get_time',
                        match_data_id: element.match_data_id,
                    }),
                );
            } else {
                const match_hour = document.createElement('p');
                match_hour.style.margin = '0';
                match_hour.style.marginBottom = '12px';
                match_hour.style.fontSize = '22px';
                match_hour.style.fontWeight = '600';
                match_hour.innerHTML = element.start_time;

                match_middle_container.appendChild(match_hour);
            }
            match_container.appendChild(match_middle_container);

            const away_team_container = document.createElement('div');
            away_team_container.classList.add('flex-column');
            away_team_container.style.width = '128px';

            const away_team_logo = document.createElement('img');
            away_team_logo.style.objectFit = 'contain';
            away_team_logo.src = element.away_team_logo;
            away_team_logo.style.width = '64px';
            away_team_logo.style.height = '64px';

            const away_team_name = document.createElement('p');
            away_team_name.style.margin = '0';
            away_team_name.style.marginTop = '4px';
            away_team_name.style.fontSize = '14px';
            away_team_name.style.textAlign = 'center';
            away_team_name.innerHTML = awayTeamText;

            away_team_container.appendChild(away_team_logo);
            away_team_container.appendChild(away_team_name);

            match_container.appendChild(away_team_container);

            infoContainer.appendChild(match_container);
        }
    } else {
        infoContainer.classList.add('flex-center');
        infoContainer.innerHTML =
            "<p style='text-align: center;'>Er zijn nog geen aankomende of gespeelde wedstrijden</p>";
    }
};
