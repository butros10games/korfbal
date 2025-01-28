import { setupCarousel, updateMatches, updateTeam } from '../../components/carousel';
import { initializeSocket, requestInitialData, onMessageReceived } from '../../utils/websockets';
import { setupFollowButton } from '../../components/setup_follow_button';
import { readUserId } from '../../utils/dom/';

window.addEventListener('DOMContentLoaded', () => {
    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');
    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = window.location.href;

    const user_id = readUserId();
    const matches = regex.exec(url);

    const infoContainer = document.getElementById('info-container');
    const maxLength = 14;

    const commandHandlers = {
        'wedstrijden': (data) => updateMatches(data, maxLength, infoContainer, socket),
        'teams': (data) => updateTeam(data, infoContainer),
    };

    let team_id;

    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    const WebSocketUrl = `wss://${window.location.host}/ws/club/${team_id}/`;
    const socket = initializeSocket(
        WebSocketUrl,
        onMessageReceived(commandHandlers),
        (socket) => {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', socket);
        }
    );

    setupCarousel(carousel, buttons, socket);
    setupFollowButton(user_id, socket);
});
