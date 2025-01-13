import {
    setupCarousel,
    updateMatches,
    updatePlayers,
    updateStatistics,
} from '../../components/carousel/index.js';
import { initializeSocket, requestInitialData, onMessageReceived } from '../../utils/websockets/index.js';
import { setupFollowButton } from '../../components/setup_follow_button';

window.addEventListener('DOMContentLoaded', () => {
    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');

    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = window.location.href;

    const user_id = document.getElementById('user_id').innerText.trim();
    const matches = regex.exec(url);

    const infoContainer = document.getElementById('info-container');
    const maxLength = 14;
    const commandHandlers = {
        'wedstrijden': (data) => updateMatches(data, maxLength, infoContainer),
        'stats': (data) => updateStatistics(data.data, infoContainer),
        'spelers': (data) => updatePlayers(data, infoContainer),
    };

    let team_id;

    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    const WebSocketUrl = `wss://${window.location.host}/ws/teams/${team_id}/`;
    const socket = initializeSocket(WebSocketUrl, onMessageReceived(commandHandlers));

    if (socket) {
        socket.onopen = function () {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', socket, { user_id: user_id });
        };
    } else {
        console.error('Failed to initialize WebSocket connection.');
    }

    setupCarousel(carousel, buttons, socket, { user_id: user_id }, 'get_stats');
    setupFollowButton(user_id, socket);
});
