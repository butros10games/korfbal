import { truncateMiddle } from '../../utils';
import {
    matchPoints,
    createEventTypeDiv,
    createMidsectionDiv,
    createScoreDiv,
    getFormattedTime,
} from '../../utils/events';

export const updateEvents = function (data, infoContainer, match_id) {
    const events = data.events;
    const home_team_id = data.home_team_id;
    let home = 0,
        away = 0;

    const eventContainer = document.createElement('div');
    eventContainer.classList.add('event-container');

    if (events.length > 0) {
        events.forEach((event) => {
            const eventDiv = document.createElement('div');
            eventDiv.classList.add('event', 'flex-row');

            if (event.type === 'goal') {
                const eventTypeDiv = createEventTypeDiv(
                    event.name,
                    '64px',
                    event.for_team ? '#4CAF50' : 'rgba(235, 0, 0, 0.7)',
                );
                const score = matchPoints(event, home, away, home_team_id);
                home = score[0];
                away = score[1];
                const midsectionDiv = createMidsectionDiv(
                    event.goal_type + ' ("' + event.time + '")',
                    truncateMiddle(event.player, 20),
                );
                const scoreDiv = createScoreDiv(home + '-' + away, '84px');

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(scoreDiv);
            } else if (event.type === 'substitute') {
                const eventTypeDiv = createEventTypeDiv(event.name, '64px', '#eb9834');
                const midsectionDiv = createMidsectionDiv(
                    '("' + event.time + '")',
                    truncateMiddle(event.player_in, 15) +
                        ' --> ' +
                        truncateMiddle(event.player_out, 15),
                );
                const endSectionDiv = document.createElement('div');
                endSectionDiv.style.width = '84px'; // For spacing/alignment purposes

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(endSectionDiv);
            } else if (event.type === 'intermission') {
                const eventTypeDiv = createEventTypeDiv(event.name, '64px', '#2196F3');
                const midsectionDiv = createMidsectionDiv(
                    '("' + event.time + '")',
                    getFormattedTime(event),
                );
                const endSectionDiv = document.createElement('div');
                endSectionDiv.style.width = '84px'; // For spacing/alignment purposes

                eventDiv.appendChild(eventTypeDiv);
                eventDiv.appendChild(midsectionDiv);
                eventDiv.appendChild(endSectionDiv);
            }

            eventContainer.appendChild(eventDiv);
        });
    } else {
        const textElement = document.createElement('p');
        textElement.classList.add('flex-center');
        textElement.innerHTML = '<p>Geen events gevonden.</p>';
        eventContainer.appendChild(textElement);
    }

    // Adding the tracker button if required
    if (data.access && data.status !== 'finished') {
        const buttonContainer = document.createElement('div');
        buttonContainer.classList.add('flex-center');
        buttonContainer.style.marginTop = '12px';

        const trackerButton = document.createElement('a');
        trackerButton.classList.add('tracker-button');
        trackerButton.href = '/match/selector/' + match_id + '/';
        trackerButton.innerHTML = 'bijhouden';
        trackerButton.style.marginBottom = '12px';

        buttonContainer.appendChild(trackerButton);
        eventContainer.appendChild(buttonContainer);
    }

    infoContainer.appendChild(eventContainer);
};
