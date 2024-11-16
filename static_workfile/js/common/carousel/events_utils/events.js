"use strict";

// Helper function to create the event type div
export const createEventTypeDiv = function(type, width, backgroundColor) {
    const eventTypeDiv = document.createElement("div");
    eventTypeDiv.classList.add("event-type", "flex-center");
    eventTypeDiv.innerHTML = type;
    eventTypeDiv.style.width = width;
    eventTypeDiv.style.backgroundColor = backgroundColor;
    return eventTypeDiv;
};

// Helper function to create the midsection div with description and player info
export const createMidsectionDiv = function(description, playerText) {
    const midsectionDiv = document.createElement("div");
    midsectionDiv.classList.add("flex-column");
    midsectionDiv.style.justifyContent = 'center';

    const descriptionDiv = document.createElement("div");
    descriptionDiv.classList.add("description");
    descriptionDiv.innerHTML = description;

    const playerName = document.createElement("p");
    playerName.innerHTML = playerText;
    playerName.style.margin = "0";

    midsectionDiv.appendChild(descriptionDiv);
    midsectionDiv.appendChild(playerName);

    return midsectionDiv;
};

// Helper function to create score div
export const createScoreDiv = function(score, width) {
    const currentScoreDiv = document.createElement("div");
    currentScoreDiv.classList.add("current-score");
    currentScoreDiv.classList.add("flex-column");
    currentScoreDiv.style.justifyContent = 'center';
    currentScoreDiv.innerHTML = score;
    currentScoreDiv.style.width = width;
    return currentScoreDiv;
};

export const getFormattedTime = function(event) {
    const timeout_div = document.createElement("p");
    timeout_div.style.margin = "0";
    timeout_div.style.fontSize = "16px";

    const start_time = new Date(event.start_time);
    if (event.end_time === null) {
        timeout_div.innerHTML = start_time.getHours().toString().padStart(2, '0') + ":" + start_time.getMinutes().toString().padStart(2, '0');
    } else {
        const end_time = new Date(event.end_time);
        timeout_div.innerHTML = start_time.getHours().toString().padStart(2, '0') + ":" + start_time.getMinutes().toString().padStart(2, '0') + " - " + end_time.getHours().toString().padStart(2, '0') + ":" + end_time.getMinutes().toString().padStart(2, '0');
    }
    return timeout_div.outerHTML;
};