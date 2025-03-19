import { CountdownTimer } from './countdownTimer.js';

/**
 * Creates or updates a timer based on the incoming data.
 * @param {Object} data Timer data from the server.
 * @param {CountdownTimer|null} existingTimer Existing timer instance (if any).
 * @returns {CountdownTimer|null} Updated or new CountdownTimer instance.
 */
export const timer_data = function(data, existingTimer, counterId) {
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
            counterId,
            data.server_time,
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
            counterId,
            data.server_time,
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
            counterId,
            data.server_time,
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
export const pause = function(data, existingTimer) {
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
export const part_end = function(data, existingTimer) {
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
