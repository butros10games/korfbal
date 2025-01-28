import {
    setupCarousel,
    updateMatches,
    updateTeam,
    updateSettings,
    updateGoalStats,
} from '../../components/carousel';
import { setupProfilePicture } from '../../components/profile_picture';
import { initializeSocket, requestInitialData, onMessageReceived } from '../../utils/websockets';
import { timer_data, pause, part_end } from '../../components/countdown_timer/countdownTimerActions.js';

window.addEventListener('DOMContentLoaded', () => {
    const timers = {};

    const carousel = document.querySelector('.carousel');
    const buttons = document.querySelectorAll('.button');
    const csrfToken = document.querySelector('input[name="csrfmiddlewaretoken"]').value;

    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = window.location.href;

    const matches = regex.exec(url);

    const infoContainer = document.getElementById('info-container');
    const profilePicture = document.getElementById('profilePic-container');
    const maxLength = 14;

    const commandHandlers = {
        'settings_request': (data) => {
            cleanDom(infoContainer, profilePicture);
            updateSettings(data, infoContainer, socket);
        },
        'player_goal_stats': (data) => {
            cleanDom(infoContainer, profilePicture);
            updateGoalStats(data, infoContainer);
        },
        'settings_updated': () => {
            settingsSaved();
        },
        'teams':        (data) => {
            cleanDom(infoContainer, profilePicture);
            updateTeam(data, infoContainer);
        },
        'matches':      (data) => {
            cleanDom(infoContainer, profilePicture);
            updateMatches(data, maxLength, infoContainer, socket);
        },
        'timer_data':   (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = timer_data(data, currentTimer, `counter_${data.match_data_id}`);
        },
        'pause':        (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = pause(data, currentTimer, `counter_${data.match_data_id}`);
        },
        'part_end':     (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = part_end(data, currentTimer, `counter_${data.match_data_id}`);
        },
    };

    let player_id;

    if (matches) {
        player_id = matches[1];
        console.log(player_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    const WebSocketUrl = `wss://${window.location.host}/ws/profile/${player_id}/`;
    const socket = initializeSocket(
        WebSocketUrl,
        onMessageReceived(commandHandlers),
        (socket) => {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', socket);
        }
    );
    
    setupCarousel(carousel, buttons, socket);
    setupProfilePicture(csrfToken);
});

function settingsSaved() {
    const saveButtonText = document.getElementById('save-button-text');
    saveButtonText.innerHTML = 'Saved!';
    saveButtonText.style.color = '#fff';

    const saveButton = document.querySelector('.save-button');
    saveButton.classList.remove('loading');

    setTimeout(() => {
        saveButtonText.innerHTML = 'Save';
        saveButtonText.style.color = '';
    }, 1500);
}

function cleanDom(element, profilePicture) {
    element.innerHTML = '';
    element.classList.remove('flex-center');
    element.classList.remove('flex-start-wrap');

    profilePicture.classList.remove('active-img');
}
