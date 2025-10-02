/**
 * Common utilities for Korfbal feature modules
 */

/**
 * Extract UUID from current URL
 * @returns {string|null} The UUID if found, null otherwise
 */
export function extractUuidFromUrl() {
    const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
    const url = globalThis.location.href;
    const matches = regex.exec(url);
    return matches ? matches[1] : null;
}

/**
 * Get CSRF token from DOM
 * @returns {string|null} The CSRF token if found
 */
export function getCsrfToken() {
    const tokenInput = document.querySelector('input[name="csrfmiddlewaretoken"]');
    return tokenInput ? tokenInput.value : null;
}

/**
 * Common DOM elements setup for detail pages
 * @returns {object} Object containing common DOM elements
 */
export function setupCommonElements() {
    return {
        carousel: document.querySelector('.carousel'),
        buttons: document.querySelectorAll('.button'),
        timers: {},
    };
}

/**
 * Common initialization for feature detail pages
 * @returns {object} Object containing common setup data
 */
export function initializeDetailPage() {
    const uuid = extractUuidFromUrl();
    const csrfToken = getCsrfToken();
    const elements = setupCommonElements();

    return {
        uuid,
        csrfToken,
        ...elements,
    };
}
