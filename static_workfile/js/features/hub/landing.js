import {
    part_end,
    pause,
    timer_data,
} from '../../components/countdown_timer/countdownTimerActions.js';
import { initializeSocket, onMessageReceived } from '../../utils/websockets/index.js';

globalThis.addEventListener('DOMContentLoaded', () => {
    const timers = {};
    const match_id = document.getElementById('match_id').innerText;

    if (!match_id) {
        console.log('No match_id found in the URL.');
        return;
    }

    const commandHandlers = {
        timer_data: (data) => {
            console.log('timer data');
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = timer_data(data, currentTimer, 'counter');
        },
        pause: (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = pause(data, currentTimer, 'counter');
        },
        part_end: (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = part_end(data, currentTimer, 'counter');
        },
    };

    const protocol = globalThis.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const WebSocketUrl = `${protocol}//${globalThis.location.host}/ws/match/${match_id}/`;
    initializeSocket(WebSocketUrl, onMessageReceived(commandHandlers), (socket) => {
        console.log('WebSocket connection established, sending initial data...');
        socket.send(JSON.stringify({ command: 'get_time' }));
    });
});
