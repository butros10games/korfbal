import {
    setupCarousel,
    updateMatches,
    updateTeam,
} from '../../components/carousel/index.js';
import {
    part_end,
    pause,
    timer_data,
} from '../../components/countdown_timer/countdownTimerActions.js';
import { setupFollowButton } from '../../components/setup_follow_button/index.js';
import { readUserId } from '../../utils/dom/index.js';
import {
    initializeSocket,
    onMessageReceived,
    requestInitialData,
} from '../../utils/websockets/index.js';

globalThis.addEventListener('DOMContentLoaded', () => {
    const timers = {};

    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');

    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = globalThis.location.href;

    const user_id = readUserId();
    const matches = regex.exec(url);

    const infoContainer = document.getElementById('info-container');
    const maxLength = 14;

    const commandHandlers = {
        matches: (data) => updateMatches(data, maxLength, infoContainer, socket),
        teams: (data) => updateTeam(data, infoContainer),
        timer_data: (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = timer_data(
                data,
                currentTimer,
                `counter_${data.match_data_id}`,
            );
        },
        pause: (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = pause(
                data,
                currentTimer,
                `counter_${data.match_data_id}`,
            );
        },
        part_end: (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = part_end(
                data,
                currentTimer,
                `counter_${data.match_data_id}`,
            );
        },
    };

    let team_id;

    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    const WebSocketUrl = `wss://${globalThis.location.host}/ws/club/${team_id}/`;
    const socket = initializeSocket(
        WebSocketUrl,
        onMessageReceived(commandHandlers),
        (ws) => {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', ws);
        },
    );

    setupCarousel(carousel, buttons, socket);
    setupFollowButton(user_id, socket);
});
