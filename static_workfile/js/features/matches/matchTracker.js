import { truncateMiddle } from '../../utils/index.js';
import {
    createEventTypeDiv,
    createMidsectionDiv,
    createScoreDiv,
    getFormattedTime,
} from '../../utils/events/index.js';
import {
    resetSwipe,
    setupSwipeDelete,
    deleteButtonSetup,
} from '../../components/swipe_delete/index.js';
import { CountdownTimer } from '../../components/countdown_timer/index.js';
import { updatePlayerGroups } from '../../components/carousel/index.js';
import { initializeSocket, onMessageReceived } from '../../utils/websockets/index.js';
import {
    scoringButtonSetup,
    shotButtonReg,
} from '../../components/scoring_button/index.js';
import { sharedData } from './sharedData.js';

let firstUUID;
let secondUUID;
const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/g;
const url = window.location.href;

let playersDiv;

let timer = null;

let playerSwitchData;

window.addEventListener('DOMContentLoaded', () => {
    const eventsDiv = document.getElementById('match-event');
    playersDiv = document.getElementById('players');

    const matches = url.match(regex);

    const actions = [
        { id: 'switch-button', name: 'Wissel', action: () => playerSwitch(socket) },
        { id: 'timeout', name: 'timeout', action: () => sendTimeout(socket) },
    ];

    console.log(matches);

    if (matches && matches.length >= 2) {
        firstUUID = matches[0];
        secondUUID = matches[1];
        console.log('First UUID:', firstUUID);
        console.log('Second UUID:', secondUUID);
    } else {
        console.log('Not enough UUIDs found in the URL.');
    }

    const startStopButton = document.getElementById('start-stop-button');

    const commandHandlers = {
        last_event: (data) => lastEvent(data, eventsDiv),
        playerGroups: (data) => playerGroups(data, socket, eventsDiv, actions),
        player_shot_change: (data) => updatePlayerShot(data),
        player_goal_change: (data) => updatePlayerGoals(data),
        goal_types: (data) => showGoalTypes(data, socket),
        timer_data: (data) => timerData(data, startStopButton),
        pause: (data) => pauseTimer(data, startStopButton),
        team_goal_change: (data) => teamGoalChangeFunction(data),
        non_active_players: (data) => showReservePlayer(data, socket),
        player_change: (data) => playerChange(data, socket),
        part_end: (data) => partEnd(data, startStopButton),
        match_end: (data) => matchEnd(data, startStopButton),
        error: (data) => errorProcessing(data),
    };

    const WebSocketUrl = `wss://${window.location.host}/ws/match/tracker/${firstUUID}/${secondUUID}/`;
    const socket = initializeSocket(
        WebSocketUrl,
        onMessageReceived(commandHandlers),
        (ws) => {
            console.log('WebSocket connection established, sending initial data...');
            requestInitialData(ws);
        },
    );

    load_icon_small(eventsDiv);
    load_icon(playersDiv);

    initializeButtons(socket);
    scoringButtonSetup(socket);
    startStopButtonSetup(socket);

    setupSwipeDelete();
    deleteButtonSetup(socket);
});

function initializeButtons(socket) {
    const buttons = document.getElementsByClassName('button');
    for (const element of buttons) {
        element.addEventListener('click', () => {
            const data = {
                command: 'event',
                event: element.id,
            };
            socket.send(JSON.stringify(data));
        });
    }

    const endHalfButton = document.getElementById('end-half-button');
    endHalfButton.addEventListener('click', () => {
        const data = {
            command: 'part_end',
        };
        socket.send(JSON.stringify(data));
    });
}

function startStopButtonSetup(socket) {
    const startStopButton = document.getElementById('start-stop-button');

    startStopButton.addEventListener('click', () => {
        const data = {
            command: 'start/pause',
        };

        socket.send(JSON.stringify(data));
    });
}

function requestInitialData(socket) {
    socket.send(
        JSON.stringify({
            command: 'playerGroups',
        }),
    );

    socket.send(
        JSON.stringify({
            command: 'last_event',
        }),
    );

    socket.send(
        JSON.stringify({
            command: 'get_time',
        }),
    );
}

function errorProcessing(data) {
    if (data.error === 'match is paused') {
        // give a notification like popup that the match is paused
        const overlay = document.createElement('div');
        overlay.id = 'overlay';
        overlay.classList.add('overlay');

        const popupElements = createPopup('De wedstrijd is gepauzeerd.');
        const popup = popupElements[0];
        const popupButton = popupElements[1];

        popupButton.addEventListener('click', () => {
            // Remove the popup and overlay when the close button is clicked
            overlay.remove();

            // remove the scroll lock
            document.body.style.overflow = '';
        });

        popup.appendChild(popupButton);

        // Append the popup to the overlay
        overlay.appendChild(popup);

        // Append the overlay to the body to cover the entire screen
        document.body.appendChild(overlay);

        // Disable scrolling on the body while the overlay is open
        document.body.style.overflow = 'hidden';
    }
}

function lastEvent(data, eventsDiv) {
    cleanDom(eventsDiv);
    resetSwipe();

    updateEvent(data);
}

function playerGroups(data, socket, eventsDiv, actions) {
    cleanDom(eventsDiv);
    cleanDom(playersDiv);

    if (data.match_active) {
        showPlayerGroups(data, socket);
        showActionMenu(actions);

        shotButtonReg('home', socket);
        shotButtonReg('away', socket);
    } else {
        updatePlayerGroups(data, playersDiv, socket); // imported from matches/common/updatePlayerGroups.js
    }
}

function timerData(data, startStopButton) {
    // remove the timer if it exists
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
            true,
            'counter',
            data.server_time,
        );
        timer.start();

        // set the pause button to pause
        startStopButton.innerHTML = 'Pause';
        document.getElementById('timeout').classList.remove('action-red');
    } else if (data.type === 'pause') {
        timer = new CountdownTimer(
            data.time,
            data.length * 1000,
            data.calc_to,
            data.pause_length * 1000,
            true,
            'counter',
            data.server_time,
        );
        timer.stop();

        // set the pause button to start
        startStopButton.innerHTML = 'Start';
        document.getElementById('timeout').classList.add('action-red');
    } else if (data.type === 'start') {
        timer = new CountdownTimer(
            data.time,
            data.length * 1000,
            null,
            0,
            true,
            'counter',
            data.server_time,
        );
        timer.start();

        startStopButton.innerHTML = 'Pause';
        document.getElementById('timeout').classList.add('action-red');
    }
}

function pauseTimer(data, startStopButton) {
    if (data.pause === true) {
        timer.stop();
        console.log('Timer paused');

        // set the pause button to start
        startStopButton.innerHTML = 'Start';
    } else if (data.pause === false) {
        timer.start(data.pause_time);
        console.log('Timer resumed');

        // set the pause button to pause
        startStopButton.innerHTML = 'Pause';
    }
}

function teamGoalChangeFunction(data) {
    teamGoalChange(data);

    // remove overlay
    const overlay = document.getElementById('overlay');
    if (overlay) {
        overlay.remove();
    }

    // remove the color change from the buttons
    const activatedButton = document.querySelector('.activated');
    if (activatedButton) {
        activatedButton.click();
    }
}

function partEnd(data, startStopButton) {
    const periode_p = document.getElementById('periode_number');
    periode_p.innerHTML = data.part;

    // reset the timer
    timer.stop();

    // destroy the timer
    timer = null;

    const timer_p = document.getElementById('counter');

    // convert seconds to minutes and seconds
    const minutes = data.part_length / 60;
    const seconds = data.part_length % 60;

    timer_p.innerHTML = minutes + ':' + seconds.toString().padStart(2, '0');

    // hide the end half button
    const endHalfButton = document.getElementById('end-half-button');
    endHalfButton.style.display = 'none';

    // change the start/pause button to start
    startStopButton.innerHTML = 'start';
}

function matchEnd(data, startStopButton) {
    // remove the timer
    if (timer) {
        timer.stop();
        timer = null;
    }

    // set the pause button to start
    startStopButton.innerHTML = 'Match ended.';

    // Add an overlay with the match end and a button when pressed it goes back to the match detail page
    const overlay_ended = document.createElement('div');
    overlay_ended.id = 'overlay';
    overlay_ended.classList.add('overlay');

    const popupElements = createPopup('De wedstrijd is afgelopen.');
    const popup = popupElements[0];
    const popupButton = popupElements[1];

    popupButton.addEventListener('click', () => {
        // Remove the popup and overlay when the close button is clicked
        overlay_ended.remove();

        // remove the scroll lock
        document.body.style.overflow = '';

        // go back to the match detail page
        window.location.href = '/match/' + data.match_id + '/';
    });

    popup.appendChild(popupButton);

    // Append the popup to the overlay
    overlay_ended.appendChild(popup);

    // Append the overlay to the body to cover the entire screen
    document.body.appendChild(overlay_ended);

    // Disable scrolling on the body while the overlay is open
    document.body.style.overflow = 'hidden';
}

function createPopup(popupTextData) {
    // Create the popup container
    const popup = document.createElement('div');
    popup.classList.add('popup');

    const popupText = document.createElement('p');
    popupText.innerHTML = popupTextData;
    popupText.style.margin = '0';
    popupText.style.fontSize = '20px';
    popupText.style.fontWeight = '600';
    popupText.style.marginBottom = '12px';

    popup.appendChild(popupText);

    const popupButton = document.createElement('button');
    popupButton.classList.add('button');
    popupButton.innerHTML = 'OK';
    popupButton.style.margin = '0';
    popupButton.style.width = '100%';
    popupButton.style.height = '42px';
    popupButton.style.fontSize = '16px';
    popupButton.style.fontWeight = '600';
    popupButton.style.marginBottom = '12px';
    popupButton.style.background = 'var(--button-color)';
    popupButton.style.color = 'var(--text-color)';
    popupButton.style.border = 'none';
    popupButton.style.borderRadius = '4px';
    popupButton.style.cursor = 'pointer';
    popupButton.style.userSelect = 'none';

    return [popup, popupButton];
}

function load_icon(element) {
    element.classList.add('flex-center');
    element.innerHTML =
        "<div id='load_icon' class='lds-ring'><div></div><div></div><div></div><div></div></div>";
}

function load_icon_small(element) {
    element.classList.add('flex-center');
    element.innerHTML =
        "<div id='load_icon' class='small-lds-ring'><div></div><div></div><div></div><div></div></div>";
}

function cleanDom(element) {
    element.innerHTML = '';
    element.classList.remove('flex-center');
    element.classList.remove('flex-start-wrap');
}

function playerChange(data, socket) {
    // look for the player button
    const playerButtonData = document.getElementById(data.player_out_id);

    playerButtonData.id = data.player_in_id;
    playerButtonData.querySelector('p').innerHTML = data.player_in;

    // change the player shot registration points
    const shots_for = playerButtonData.querySelector('#shots-for');
    const shots_against = playerButtonData.querySelector('#shots-against');

    const goals_for = playerButtonData.querySelector('#goals-for');
    const goals_against = playerButtonData.querySelector('#goals-for');

    shots_for.innerHTML = data.player_in_shots_for;
    shots_against.innerHTML = data.player_in_shots_against;

    goals_for.innerHTML = data.player_in_goals_for;
    goals_against.innerHTML = data.player_in_goals_against;

    playerSwitch(socket);

    // remove the overlay
    const overlay = document.getElementById('overlay');
    overlay.remove();
}

function teamGoalChange(data) {
    const first_team = document.getElementById('home-score');
    const firstTeamP = first_team.querySelector('p');

    const second_team = document.getElementById('away-score');
    const secondTeamP = second_team.querySelector('p');

    firstTeamP.innerHTML = data.goals_for;
    secondTeamP.innerHTML = data.goals_against;
}

function showGoalTypes(data, socket) {
    // Create the overlay container
    const overlay = document.createElement('div');
    overlay.id = 'overlay';
    overlay.classList.add('overlay');

    // Create the popup container
    const popup = document.createElement('div');
    popup.classList.add('popup');

    // Create the content for the popup
    const goalTypesContainer = document.createElement('div');
    goalTypesContainer.classList.add('goal-types-container');
    goalTypesContainer.style.display = 'flex';
    goalTypesContainer.style.flexWrap = 'wrap'; // Add this line to wrap the buttons to a second line

    const TopLineContainer = document.createElement('div');
    TopLineContainer.classList.add('flex-row');
    TopLineContainer.style.marginBottom = '12px';

    const goalTypesTitle = document.createElement('p');
    goalTypesTitle.innerHTML = 'Doelpunt type';
    goalTypesTitle.style.margin = '0';

    TopLineContainer.appendChild(goalTypesTitle);

    // Create a close button for the popup
    const closeButton = document.createElement('button');
    closeButton.classList.add('close-button');
    closeButton.innerHTML = 'Close';
    closeButton.addEventListener('click', () => {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();
    });

    TopLineContainer.appendChild(closeButton);

    goalTypesContainer.appendChild(TopLineContainer);

    for (const goalType of data.goal_types) {
        const goalTypeDiv = document.createElement('div');
        goalTypeDiv.classList.add('goal-type');
        goalTypeDiv.classList.add('flex-center');
        goalTypeDiv.style.flexGrow = '1';
        goalTypeDiv.style.flexBasis = 'calc(50% - 32px)';
        goalTypeDiv.style.textAlign = 'center';
        goalTypeDiv.style.margin = '0 12px 6px 12px';
        goalTypeDiv.style.width = 'calc(100% - 12px)';
        goalTypeDiv.style.background = goalType.color;

        const goalTypeTitle = document.createElement('p');
        goalTypeTitle.classList.add('flex-center');
        goalTypeTitle.innerHTML = truncateMiddle(goalType.name, 16);
        goalTypeTitle.style.margin = '0';
        goalTypeTitle.style.fontSize = '16px';
        goalTypeTitle.style.background = 'var(--button-color)';
        goalTypeTitle.style.color = 'var(--text-color)';
        goalTypeTitle.style.padding = '6px';
        goalTypeTitle.style.borderRadius = '4px';
        goalTypeTitle.style.width = '100%';
        goalTypeTitle.style.height = '42px';
        goalTypeTitle.style.cursor = 'pointer';
        goalTypeTitle.style.userSelect = 'none';

        goalTypeDiv.addEventListener('click', () => {
            const last_goal_Data = sharedData.last_goal_Data;

            const data_send = {
                command: 'goal_reg',
                goal_type: goalType.id,
                player_id: last_goal_Data.player_id,
                for_team: last_goal_Data.for_team,
            };

            socket.send(JSON.stringify(data_send));
        });

        goalTypeDiv.appendChild(goalTypeTitle);

        goalTypesContainer.appendChild(goalTypeDiv);
    }

    // Append the close button and goalTypesContainer to the popup
    popup.appendChild(goalTypesContainer);

    // Append the popup to the overlay
    overlay.appendChild(popup);

    // Append the overlay to the body to cover the entire screen
    document.body.appendChild(overlay);

    // Disable scrolling on the body while the overlay is open
    document.body.style.overflow = 'hidden';
}

function showReservePlayer(data, socket) {
    // Create the overlay container
    const overlay = document.createElement('div');
    overlay.id = 'overlay';
    overlay.classList.add('overlay');

    // Create the popup container
    const popup = document.createElement('div');
    popup.classList.add('popup');

    // Create the content for the popup
    const PlayersContainer = document.createElement('div');
    PlayersContainer.classList.add('goal-types-container');
    PlayersContainer.style.display = 'flex';
    PlayersContainer.style.flexWrap = 'wrap'; // Add this line to wrap the buttons to a second line

    const TopLineContainer = document.createElement('div');
    TopLineContainer.classList.add('flex-row');
    TopLineContainer.style.marginBottom = '12px';

    const PlayersTitle = document.createElement('p');
    PlayersTitle.innerHTML = 'Reserve spelers';
    PlayersTitle.style.margin = '0';

    TopLineContainer.appendChild(PlayersTitle);

    // Create a close button for the popup
    const closeButton = document.createElement('button');
    closeButton.classList.add('close-button');
    closeButton.innerHTML = 'Close';
    closeButton.addEventListener('click', () => {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();
    });

    TopLineContainer.appendChild(closeButton);

    PlayersContainer.appendChild(TopLineContainer);

    for (const Player of data.players) {
        const PlayerDiv = document.createElement('div');
        PlayerDiv.classList.add('goal-type');
        PlayerDiv.classList.add('flex-center');
        PlayerDiv.style.flexGrow = '1';
        PlayerDiv.style.flexBasis = 'calc(50% - 32px)';
        PlayerDiv.style.textAlign = 'center';
        PlayerDiv.style.margin = '0 12px 6px 12px';
        PlayerDiv.style.width = 'calc(100% - 12px)';
        PlayerDiv.style.background = Player.color;

        const PlayerTitle = document.createElement('p');
        PlayerTitle.classList.add('flex-center');
        PlayerTitle.innerHTML = truncateMiddle(Player.name, 16);
        PlayerTitle.style.margin = '0';
        PlayerTitle.style.fontSize = '16px';
        PlayerTitle.style.background = 'var(--button-color)';
        PlayerTitle.style.color = 'var(--text-color)';
        PlayerTitle.style.padding = '6px';
        PlayerTitle.style.borderRadius = '4px';
        PlayerTitle.style.width = '100%';
        PlayerTitle.style.height = '42px';
        PlayerTitle.style.cursor = 'pointer';
        PlayerTitle.style.userSelect = 'none';

        PlayerDiv.addEventListener('click', () => {
            const data_send = {
                command: 'substitute_reg',
                new_player_id: Player.id,
                old_player_id: playerSwitchData.player_id,
            };

            console.log(data_send);

            socket.send(JSON.stringify(data_send));
        });

        PlayerDiv.appendChild(PlayerTitle);

        PlayersContainer.appendChild(PlayerDiv);
    }

    // Append the close button and PlayersContainer to the popup
    popup.appendChild(PlayersContainer);

    // Append the popup to the overlay
    overlay.appendChild(popup);

    // Append the overlay to the body to cover the entire screen
    document.body.appendChild(overlay);

    // Disable scrolling on the body while the overlay is open
    document.body.style.overflow = 'hidden';
}

function updateEvent(data) {
    const event = data.last_event;
    const eventsDiv = document.createElement('div');
    eventsDiv.style.display = 'flex';
    eventsDiv.style.justifyContent = 'space-between';
    eventsDiv.style.width = '100%';
    eventsDiv.style.height = '100%';

    switch (event.type) {
        case 'goal': {
            const eventTypeDiv = createEventTypeDiv(
                event.name,
                '64px',
                event.for_team ? '#4CAF50' : 'rgba(235, 0, 0, 0.7)',
            );
            const midsectionDiv = createMidsectionDiv(
                event.shot_type + ' ("' + event.time + '")',
                truncateMiddle(event.player, 20),
            );
            const scoreDiv = createScoreDiv(
                event.goals_for + '-' + event.goals_against,
                '84px',
            );

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(scoreDiv);
            break;
        }
        case 'substitute': {
            const eventTypeDiv = createEventTypeDiv(event.name, '64px', '#eb9834');
            const midsectionDiv = createMidsectionDiv(
                '("' + event.time + '")',
                truncateMiddle(event.player_in, 15) +
                    ' --> ' +
                    truncateMiddle(event.player_out, 15),
            );
            const endSectionDiv = document.createElement('div');
            endSectionDiv.style.width = '84px'; // For spacing/alignment purposes

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(endSectionDiv);
            break;
        }
        case 'pause': {
            const eventTypeDiv = createEventTypeDiv(event.name, '64px', '#2196F3');
            const midsectionDiv = createMidsectionDiv(
                '("' + event.time + '")',
                getFormattedTime(event),
            );
            const endSectionDiv = document.createElement('div');
            endSectionDiv.style.width = '84px'; // For spacing/alignment purposes

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(endSectionDiv);
            break;
        }
        case 'shot': {
            const eventTypeDiv = createEventTypeDiv(
                event.name,
                '64px',
                event.for_team ? '#43ff644d' : '#eb00004d',
            );
            const midsectionDiv = createMidsectionDiv(
                '("' + event.time + '")',
                truncateMiddle(event.player, 20),
            );
            const endSectionDiv = document.createElement('div');
            endSectionDiv.style.width = '84px'; // For spacing/alignment purposes

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(endSectionDiv);
            break;
        }
        case 'attack': {
            const eventTypeDiv = createEventTypeDiv(event.name, '64px', '#43ff644d');
            const midsectionDiv = createMidsectionDiv(
                '("' + event.time + '")',
                truncateMiddle(event.team, 20),
            );
            const endSectionDiv = document.createElement('div');
            endSectionDiv.style.width = '84px';

            eventsDiv.appendChild(eventTypeDiv);
            eventsDiv.appendChild(midsectionDiv);
            eventsDiv.appendChild(endSectionDiv);
            break;
        }
        case 'no_event': {
            const textElement = document.createElement('p');
            textElement.classList.add('flex-center');
            textElement.style.margin = '0';
            textElement.style.height = '64px';
            textElement.innerHTML = 'Geen events gevonden.';
            textElement.style.width = '100%';

            eventsDiv.appendChild(textElement);
            break;
        }
        default: {
            console.warn('Unknown event type: ', event.type);
            const defaultElement = document.createElement('p');
            defaultElement.innerHTML = 'Onbekend event type: ' + event.type;
            eventsDiv.appendChild(defaultElement);
            break;
        }
    }

    // Append eventsDiv to the container (assuming there's a container in the DOM to append it to)
    const eventContainer = document.getElementById('match-event'); // Replace with the actual container ID
    if (eventContainer) {
        eventContainer.appendChild(eventsDiv);
    } else {
        console.error('Event container not found');
    }
}

function updatePlayerShot(data) {
    const playerGroupsElement = document.getElementsByClassName('player-group-players');

    for (const playerGroup of playerGroupsElement) {
        // Use attribute selector syntax
        const playerDiv = playerGroup.querySelector(`[id="${data.player_id}"]`);

        if (playerDiv) {
            const shotsFor = playerDiv.querySelector('#shots-for');
            const shotsAgainst = playerDiv.querySelector('#shots-against');

            shotsFor.innerHTML = data.shots_for;
            shotsAgainst.innerHTML = data.shots_against;
        }
    }
}

function updatePlayerGoals(data) {
    const playerGroupsElement = document.getElementsByClassName('player-group-players');

    for (const playerGroup of playerGroupsElement) {
        // Use attribute selector syntax
        const playerDiv = playerGroup.querySelector(`[id="${data.player_id}"]`);

        if (playerDiv) {
            const goalsFor = playerDiv.querySelector('#goals-for');
            const goalsAgainst = playerDiv.querySelector('#goals-against');

            goalsFor.innerHTML = data.goals_for;
            goalsAgainst.innerHTML = data.goals_against;
        }
    }
}

function showPlayerGroups(data, socket) {
    const homeScoreButton = document.getElementById('home-score');
    const awayScoreButton = document.getElementById('away-score');

    // Remove the activated class from the buttons and reset their color
    if (homeScoreButton.classList.contains('activated')) {
        homeScoreButton.classList.remove('activated');
        homeScoreButton.style.background = '#43ff6480';
    }

    if (awayScoreButton.classList.contains('activated')) {
        awayScoreButton.classList.remove('activated');
        awayScoreButton.style.background = 'rgba(235, 0, 0, 0.5)';
    }

    const playerGroupsData = data.playerGroups;

    const playerGroupContainer = createDivWithClass('player-group-container');

    playerGroupsData.forEach((playerGroup, index) => {
        const playerGroupDiv = createDivWithClass('player-group', 'flex-column');
        playerGroupDiv.style.marginTop = '12px';

        const playerGroupTitleDiv = createDivWithClass(
            'flex-row',
            'player-group-title',
        );
        playerGroupTitleDiv.style.marginBottom = '6px';
        playerGroupTitleDiv.style.margin = '0 12px 6px 12px';
        playerGroupTitleDiv.style.width = 'calc(100% - 24px)';

        const playerGroupTitle = document.createElement('div');
        playerGroupTitle.style.fontWeight = '600';
        playerGroupTitle.innerHTML = playerGroup.current_type;
        playerGroupTitle.id = playerGroup.id;

        playerGroupTitleDiv.appendChild(playerGroupTitle);

        if (index === 0) {
            const AttackButtonDiv = createDivWithClass('attack-button', 'flex-center');
            AttackButtonDiv.innerHTML = 'Nieuwe aanval';
            AttackButtonDiv.id = 'attack-button';
            AttackButtonDiv.style.width = '128px';

            AttackButtonDiv.addEventListener('click', () => {
                socket.send(JSON.stringify({ command: 'new_attack' }));
            });

            playerGroupTitleDiv.appendChild(AttackButtonDiv);
        }

        const playerGroupPlayers = createDivWithClass(
            'player-group-players',
            'flex-row',
        );
        playerGroupPlayers.style.flexWrap = 'wrap';
        playerGroupPlayers.style.alignItems = 'stretch';
        playerGroupPlayers.id = playerGroup.current_type;

        playerGroup.players.forEach((player) => {
            const playerDiv = createDivWithClass('player-selector', 'flex-center');
            playerDiv.id = player.id;
            playerDiv.style.flexGrow = '1';
            playerDiv.style.flexBasis = 'calc(50% - 44px)';
            playerDiv.style.padding = '0 6px';
            playerDiv.style.textAlign = 'center';
            playerDiv.style.justifyContent = 'space-between';

            const playerName = document.createElement('p');
            playerName.style.margin = '0';
            playerName.style.fontSize = '16px';
            playerName.innerHTML = truncateMiddle(player.name, 16);

            const playerShots = shotDisplay(
                player.shots_for,
                player.shots_against,
                'shots',
            );
            playerShots.style.marginLeft = 'auto';

            const playerGoals = shotDisplay(
                player.goals_for,
                player.goals_against,
                'goals',
            );

            playerDiv.appendChild(playerName);
            playerDiv.appendChild(playerShots);
            playerDiv.appendChild(playerGoals);

            playerGroupPlayers.appendChild(playerDiv);
        });

        playerGroupDiv.appendChild(playerGroupTitleDiv);
        playerGroupDiv.appendChild(playerGroupPlayers);

        playerGroupContainer.appendChild(playerGroupDiv);
    });

    playersDiv.appendChild(playerGroupContainer);
}

function shotDisplay(home, against, idType) {
    const playerShots = createDivWithClass('flex-column');
    playerShots.style.width = '20px';

    const playerShotsFor = document.createElement('p');
    playerShotsFor.id = `${idType}-for`;
    playerShotsFor.style.margin = '0';
    playerShotsFor.style.fontSize = '16px';
    playerShotsFor.style.marginBottom = '-10px';
    playerShotsFor.innerHTML = home;

    const playerShotsDivider = document.createElement('p');
    playerShotsDivider.style.margin = '0';
    playerShotsDivider.style.fontSize = '16px';
    playerShotsDivider.innerHTML = '-';

    const playerShotsAgainst = document.createElement('p');
    playerShotsAgainst.id = `${idType}-against`;
    playerShotsAgainst.style.margin = '0';
    playerShotsAgainst.style.fontSize = '16px';
    playerShotsAgainst.style.marginTop = '-10px';
    playerShotsAgainst.innerHTML = against;

    playerShots.appendChild(playerShotsFor);
    playerShots.appendChild(playerShotsDivider);
    playerShots.appendChild(playerShotsAgainst);

    return playerShots;
}

function showActionMenu(actions) {
    const actionsContainer = createDivWithClass('player-group-container');
    const actionGroupDiv = createDivWithClass('player-group', 'flex-column');
    actionGroupDiv.style.marginTop = '12px';

    const actionGroupTitleDiv = createDivWithClass('flex-row', 'player-group-title');
    actionGroupTitleDiv.style.margin = '0 12px 6px 12px';
    actionGroupTitleDiv.style.width = 'calc(100% - 24px)';

    const actionGroupTitle = document.createElement('div');
    actionGroupTitle.style.fontWeight = '600';
    actionGroupTitle.innerHTML = 'Acties';
    actionGroupTitleDiv.appendChild(actionGroupTitle);

    const actionGroupActions = createDivWithClass('player-group-players', 'flex-row');
    actionGroupActions.style.flexWrap = 'wrap';
    actionGroupActions.style.alignItems = 'stretch';

    actions.forEach((action) => {
        const actionDiv = createDivWithClass('action-selector', 'flex-center');
        actionDiv.style.flexGrow = '1';
        actionDiv.style.flexBasis = 'calc(50% - 44px)';
        actionDiv.style.padding = '0 6px';
        actionDiv.style.textAlign = 'center';
        actionDiv.id = action.id;

        const actionName = document.createElement('p');
        actionName.style.margin = '0';
        actionName.style.fontSize = '16px';
        actionName.innerHTML = truncateMiddle(action.name, 16);

        actionDiv.appendChild(actionName);
        actionDiv.addEventListener('click', action.action);

        actionGroupActions.appendChild(actionDiv);
    });

    actionGroupDiv.appendChild(actionGroupTitleDiv);
    actionGroupDiv.appendChild(actionGroupActions);
    actionsContainer.appendChild(actionGroupDiv);

    playersDiv.appendChild(actionsContainer);
}

function createDivWithClass(...classNames) {
    const div = document.createElement('div');
    classNames.forEach((className) => div.classList.add(className));
    return div;
}

function playerSwitch(socket) {
    const switchButton = document.getElementById('switch-button');

    if (switchButton.classList.contains('activated')) {
        switchButton.classList.remove('activated');
        switchButton.style.background = '';

        const playerButtons = document.getElementsByClassName('player-selector');

        Array.from(playerButtons).forEach((element) => {
            element.style.background = '';
            if (element.playerClickHandler) {
                element.removeEventListener('click', element.playerClickHandler);
                delete element.playerClickHandler; // Properly remove handler reference
            }
        });

        shotButtonReg('home', socket);
        shotButtonReg('away', socket);
    } else {
        switchButton.classList.add('activated');
        switchButton.style.background = '#4169e152';

        const playerButtons = document.getElementsByClassName('player-selector');

        Array.from(playerButtons).forEach((element) => {
            const homeScoreButton = document.getElementById('home-score');
            const awayScoreButton = document.getElementById('away-score');

            if (homeScoreButton.classList.contains('activated')) {
                homeScoreButton.classList.remove('activated');
                homeScoreButton.style.background = '#43ff6480';
            }

            if (awayScoreButton.classList.contains('activated')) {
                awayScoreButton.classList.remove('activated');
                awayScoreButton.style.background = 'rgba(235, 0, 0, 0.5)';
            }

            element.style.background = '#4169e152';
            if (element.playerClickHandler) {
                element.removeEventListener('click', element.playerClickHandler);
                delete element.playerClickHandler; // Properly remove handler reference
            }

            // Add new handler
            const playerClickHandler = function () {
                playerSwitchData = {
                    player_id: element.id,
                };

                const data = {
                    command: 'get_non_active_players',
                };

                socket.send(JSON.stringify(data));
            };

            element.playerClickHandler = playerClickHandler;
            element.addEventListener('click', playerClickHandler);
        });
    }
}

function sendTimeout(socket) {
    // Check if the class action-red is not present
    const timeoutButton = document.getElementById('timeout');

    if (!timeoutButton.classList.contains('action-red')) {
        const data = {
            command: 'timeout',
        };

        socket.send(JSON.stringify(data));
    } else {
        errorProcessing({ error: 'match is paused' });
    }
}
