import { sharedData } from '../../../matches/shared_data.js';

export const createPlayerClickHandler = function(element, team, socket) {
    return function() {
        const data = { "command": "get_goal_types" };
        const last_goal_Data = {
            "player_id": element.id,
            "time": new Date().toISOString(),
            "for_team": team === "home",
        };

        sharedData.last_goal_Data = last_goal_Data;
        socket.send(JSON.stringify(data));
    };
};