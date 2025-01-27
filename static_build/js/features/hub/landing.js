import { CountdownTimer } from '../../components/countdown_timer/index.js';
import { initializeSocket } from '../../utils/websockets/index.js';

window.addEventListener('DOMContentLoaded', () => {
    let match_id = document.getElementById('match_id').innerText;

    const WebSocketUrl = `wss://${window.location.host}/ws/match/${match_id}/`;
    initializeSocket(
        WebSocketUrl,
        (event) => onMessageReceived(event),
        (socket) => {
            console.log('WebSocket connection established, sending initial data...');
            socket.send(JSON.stringify({ command: 'get_time' }));
        }
    );
});

let timer = null;

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    console.log(data);

    switch (data.command) {
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
