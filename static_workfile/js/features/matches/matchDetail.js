import {
    setupCarousel,
    updatePlayerGroups,
    showPlayerGroups,
    updateStatistics,
    updateEvents,
} from '../../components/carousel/index.js';
import { initializeSocket, requestInitialData } from '../../utils/websockets/index.js';
import { cleanDomCarousel } from '../../utils/dom/';
import { CountdownTimer } from './common/index.js';

window.addEventListener('DOMContentLoaded', () => {
    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');
    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = window.location.href;
    const user_id = document.getElementById('user_id').innerText;
    const matches = regex.exec(url);

    let match_id;

    if (matches) {
        match_id = matches[1];
        console.log(match_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    const WebSocketUrl = `wss://${window.location.host}/ws/match/${match_id}/`;
    const socket = initializeSocket(WebSocketUrl, (event) =>
        onMessageReceived(event, match_id, user_id, socket),
    );

    if (socket) {
        socket.onopen = function () {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', socket, { user_id: user_id });
        };
    }

    setupCarousel(carousel, buttons, socket, { user_id: user_id }, 'get_stats');
});

let timer = null;

function onMessageReceived(event, match_id, user_id, socket) {
    const infoContainer = document.getElementById('info-container');

    const data = JSON.parse(event.data);
    console.log(data);

    switch (data.command) {
        case 'events': {
            cleanDomCarousel(infoContainer);

            updateEvents(data, infoContainer, match_id);
            break;
        }

        case 'playerGroups': {
            cleanDomCarousel(infoContainer);

            if (data.is_coach && !data.finished) {
                updatePlayerGroups(data, infoContainer, socket); // imported from matches/common/updatePlayerGroups.js
            } else {
                showPlayerGroups(data, infoContainer); // imported from matches/common/showPlayerGroups.js
            }
            break;
        }

        case 'team_goal_change': {
            const scoreField = document.getElementById('score');
            scoreField.innerHTML = data.goals_for + ' / ' + data.goals_against;

            break;
        }

        case 'stats': {
            updateStatistics(data.data, infoContainer, socket, user_id); // imported from common/updateStatistics.js
            break;
        }

        case 'timer_data': {
            if (timer) {
                timer.destroy();
                timer = null;
            }

            if (data.type === 'active') {
                timer = new CountdownTimer(
                    data.time,
                    data.length * 1000,
                    null,
                    data.pause_length * 1000,
                );
                timer.start();
            } else if (data.type === 'pause') {
                timer = new CountdownTimer(
                    data.time,
                    data.length * 1000,
                    data.calc_to,
                    data.pause_length * 1000,
                );
            } else if (data.type === 'start') {
                timer = new CountdownTimer(data.time, data.length * 1000, null, 0);
                timer.start();
            }

            break;
        }

        case 'pause': {
            if (data.pause === true) {
                timer.stop();
                console.log('Timer paused');
            } else if (data.pause === false) {
                timer.start(data.pause_time);
                console.log('Timer resumed');
            }

            break;
        }

        case 'part_end': {
            const periode_p = document.getElementById('periode_number');
            periode_p.innerHTML = data.part;

            timer.stop();

            timer = null;

            const timer_p = document.getElementById('counter');

            // convert seconds to minutes and seconds
            const minutes = data.part_length / 60;
            const seconds = data.part_length % 60;

            timer_p.innerHTML = minutes + ':' + seconds.toString().padStart(2, '0');
        }
    }
}
