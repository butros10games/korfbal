import { toggleButton } from './scoringButtonCore.js';

export const scoringButtonSetup = function (socket) {
    const homeScoreButton = document.getElementById('home-score');
    const awayScoreButton = document.getElementById('away-score');

    homeScoreButton.addEventListener('click', () =>
        toggleButton(homeScoreButton, 'home', socket, homeScoreButton),
    );
    awayScoreButton.addEventListener('click', () =>
        toggleButton(awayScoreButton, 'away', socket, homeScoreButton),
    );
};
