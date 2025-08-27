import {
    setupCarousel,
    updateStatistics,
    updateEvents,
} from '../../components/carousel/index.js';
import { CountdownTimer } from '../../components/countdown_timer/index.js';
import { initializeSocket, requestInitialData } from '../../utils/websockets/index.js';
import { cleanDomCarousel, readUserId } from '../../utils/dom/index.js';
import { PlayerGroupManager } from '../../components/player_group/index.js';

window.addEventListener('DOMContentLoaded', () => {
    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');
    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = window.location.href;
    const user_id = readUserId();
    const matches = regex.exec(url);

    let match_id;

    if (matches) {
        match_id = matches[1];
        console.log(match_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    console.log('Match ID:', match_id);

    const WebSocketUrl = `wss://${window.location.host}/ws/match/${match_id}/`;
    const socket = initializeSocket(
        WebSocketUrl,
        (event) => onMessageReceived(event, match_id, user_id, socket),
        (ws) => {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', ws, { user_id: user_id });
            ws.send(JSON.stringify({ command: 'get_time' }));
        },
    );

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

            const access = data.is_coach && !data.finished;

            const playerGroupManager = new PlayerGroupManager(
                data.match_id,
                data.team_id,
                'info-container',
                access,
                data.group_id_to_type_id,
                data.type_id_to_group_id,
            );
            playerGroupManager.initialize();
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
                    false,
                    'counter',
                    data.server_time,
                );
                timer.start();
            } else if (data.type === 'pause') {
                timer = new CountdownTimer(
                    data.time,
                    data.length * 1000,
                    data.calc_to,
                    data.pause_length * 1000,
                    false,
                    'counter',
                    data.server_time,
                );
            } else if (data.type === 'start') {
                timer = new CountdownTimer(
                    data.time,
                    data.length * 1000,
                    null,
                    0,
                    false,
                    'counter',
                    data.server_time,
                );
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
