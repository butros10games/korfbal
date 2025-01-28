import { initializeSocket, onMessageReceived } from '../../utils/websockets/index.js';
import { timer_data, pause, part_end } from '../../components/countdown_timer/countdownTimerActions.js';

window.addEventListener('DOMContentLoaded', () => {
    let timers = {};
    let match_id = document.getElementById('match_id').innerText;

    const commandHandlers = {
        'timer_data':   (data) => {
            console.log("timer data")
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = timer_data(data, currentTimer, `counter`);
        },
        'pause':        (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = pause(data, currentTimer, `counter`);
        },
        'part_end':     (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = part_end(data, currentTimer, `counter`);
        },
    };

    const WebSocketUrl = `wss://${window.location.host}/ws/match/${match_id}/`;
    initializeSocket(
        WebSocketUrl,
        onMessageReceived(commandHandlers),
        (socket) => {
            console.log('WebSocket connection established, sending initial data...');
            socket.send(JSON.stringify({ command: 'get_time' }));
        }
    );
});
