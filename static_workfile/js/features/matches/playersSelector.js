import { PlayerGroupManager } from '../../components/player_group/index.js';

// Instantiate and initialize the class after DOM content is loaded
document.addEventListener('DOMContentLoaded', async () => {
    const url = globalThis.location.href;

    // Extract the teamId and matchId from the URL
    const urlParts = url.split('/');
    const teamId = urlParts[urlParts.length - 2];
    const matchId = urlParts[urlParts.length - 3];

    // Pass in the ID of the container where you want all the elements appended
    const playerGroupManager = new PlayerGroupManager(
        matchId,
        teamId,
        'main-content',
        true,
    );
    playerGroupManager.initialize();
});
