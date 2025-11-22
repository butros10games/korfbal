import {
    setupCarousel,
    updateGoalStats,
    updateMatches,
    updateSettings,
    updateTeam,
} from '../../components/carousel/index.js';
import {
    part_end,
    pause,
    timer_data,
} from '../../components/countdown_timer/countdownTimerActions.js';
import { setupProfilePicture } from '../../components/profile_picture/index.js';
import {
    initializeSocket,
    onMessageReceived,
    requestInitialData,
} from '../../utils/websockets/index.js';

import { initializeDetailPage } from '../../utils/common.js';

globalThis.addEventListener('DOMContentLoaded', () => {
    const { uuid, csrfToken, carousel, buttons, timers } = initializeDetailPage();

    if (!uuid) {
        console.error('Could not extract UUID from URL');
        return;
    }

    const infoContainer = document.getElementById('info-container');
    const profilePicture = document.getElementById('profilePic-container');
    const maxLength = 14;

    const commandHandlers = {
        settings_request: (data) => {
            cleanDom(infoContainer, profilePicture);
            updateSettings(data, infoContainer, socket);
        },
        player_goal_stats: (data) => {
            cleanDom(infoContainer, profilePicture);
            updateGoalStats(data, infoContainer);
        },
        settings_updated: () => {
            settingsSaved();
        },
        teams: (data) => {
            cleanDom(infoContainer, profilePicture);
            updateTeam(data, infoContainer);
        },
        matches: (data) => {
            cleanDom(infoContainer, profilePicture);
            updateMatches(data, maxLength, infoContainer, socket);
        },
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

    let player_id;

    if (uuid) {
        player_id = uuid;
        console.log(player_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    const protocol = globalThis.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const WebSocketUrl = `${protocol}//${globalThis.location.host}/ws/profile/${player_id}/`;
    const socket = initializeSocket(
        WebSocketUrl,
        onMessageReceived(commandHandlers),
        (ws) => {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', ws);
        },
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
    element.classList.remove('flex-center', 'flex-start-wrap');

    profilePicture.classList.remove('active-img');
}
