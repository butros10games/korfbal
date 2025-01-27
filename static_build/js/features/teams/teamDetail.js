import {
    setupCarousel,
    updateMatches,
    updatePlayers,
    updateStatistics,
} from '../../components/carousel/index.js';
import { initializeSocket, requestInitialData, onMessageReceived } from '../../utils/websockets/index.js';
import { setupFollowButton } from '../../components/setup_follow_button';
import { readUserId } from '../../utils/dom/';
import { CountdownTimer } from '../../components/countdown_timer/index.js';

window.addEventListener('DOMContentLoaded', () => {
    const timers = {};
    
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
        'stats':       (data) => updateStatistics(data.data, infoContainer, socket, user_id),
        'spelers':     (data) => updatePlayers(data, infoContainer),
        'timer_data':  (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = timer_data(data, currentTimer);
        },
        'pause':       (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = pause(data, currentTimer);
        },
        'part_end':    (data) => {
            const currentTimer = timers[data.match_data_id];
            timers[data.match_data_id] = part_end(data, currentTimer);
        },
    };

    let team_id;

    if (matches) {
        team_id = matches[1];
        console.log(team_id);
    } else {
        console.log('No UUID found in the URL.');
    }

    const WebSocketUrl = `wss://${window.location.host}/ws/teams/${team_id}/`;
    const socket = initializeSocket(
        WebSocketUrl,
        onMessageReceived(commandHandlers),
        (socket) => {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData('.button.active', socket, { user_id: user_id });
        }
    );

    setupCarousel(carousel, buttons, socket, { user_id: user_id }, 'get_stats');
    setupFollowButton(user_id, socket);
});

/**
 * Creates or updates a timer based on the incoming data.
 * @param {Object} data Timer data from the server.
 * @param {CountdownTimer|null} existingTimer Existing timer instance (if any).
 * @returns {CountdownTimer|null} Updated or new CountdownTimer instance.
 */
function timer_data(data, existingTimer) {
    // If a timer already exists, destroy it before creating a new one.
    if (existingTimer) {
        existingTimer.destroy();
        existingTimer = null;
    }

    // Depending on the timer type, instantiate and/or start a new timer:
    if (data.type === 'active') {
        // 'active' means we have a running timer with a pause length
        existingTimer = new CountdownTimer(
            data.time,
            data.length * 1000,
            null,
            data.pause_length * 1000,
            false,
            `counter_${data.match_data_id}`
        );
        existingTimer.start();
    } else if (data.type === 'pause') {
        // 'pause' means the timer was paused at some point
        existingTimer = new CountdownTimer(
            data.time,
            data.length * 1000,
            data.calc_to,
            data.pause_length * 1000,
            false,
            `counter_${data.match_data_id}`
        );
        // Not explicitly starting it here, waiting for another message?
    } else if (data.type === 'start') {
        // 'start' means a new timer with no pause offset
        existingTimer = new CountdownTimer(
            data.time,
            data.length * 1000,
            null,
            0,
            false,
            `counter_${data.match_data_id}`
        );
        existingTimer.start();
    }

    return existingTimer;
}

/**
 * Pauses or resumes the timer.
 * @param {Object} data Pause data from the server (e.g. { pause: true/false }).
 * @param {CountdownTimer|null} existingTimer The existing timer instance.
 * @returns {CountdownTimer|null} Updated timer instance.
 */
function pause(data, existingTimer) {
    if (!existingTimer) {
        return existingTimer;
    }
    if (data.pause === true) {
        existingTimer.stop();
        console.log('Timer paused');
    } else if (data.pause === false) {
        // Optionally pass the updated pause_time if needed
        existingTimer.start(data.pause_time);
        console.log('Timer resumed');
    }

    return existingTimer;
}

/**
 * Ends the current part/period of the match, stops the timer, and resets display.
 * @param {Object} data Part end data from the server (e.g. { part: 2, part_length: 600 }).
 * @param {CountdownTimer|null} existingTimer The existing timer instance.
 * @returns {null} Because the old timer is ended and cleared.
 */
function part_end(data, existingTimer) {
    // If you have a unique DOM element per match_data_id, build its ID here:
    const periode_p = document.getElementById(`periode_number_${data.match_data_id}`);
    if (periode_p) {
        periode_p.innerHTML = data.part;
    }

    if (existingTimer) {
        existingTimer.stop();
    }

    // Clear out the timer entirely
    existingTimer = null;

    // Show or reset the timer text on the page
    const timer_p = document.getElementById(`counter_${data.match_data_id}`);
    if (timer_p) {
        const minutes = Math.floor(data.part_length / 60);
        const seconds = data.part_length % 60;
        timer_p.innerHTML = `${minutes}:${seconds.toString().padStart(2, '0')}`;
    }

    return existingTimer;
}
