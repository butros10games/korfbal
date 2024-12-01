export const shotButtonReg = function(team, socket) {
    const playerButtonsContainer = document.getElementById(team === "home" ? "Aanval" : "Verdediging");
    const playerButtons = playerButtonsContainer.getElementsByClassName("player-selector");
    
    // Remove event listeners from the deactivated button
    Array.from(playerButtons).forEach(element => {
        element.style.background = "";
        element.removeEventListener("click", element.playerClickHandler);
        delete element.playerClickHandler;

        // set a other click event to the player buttons to register shots
        const playerClickHandler = function() {
            const data = {
                "command": "shot_reg",
                "player_id": element.id,
                "time": new Date().toISOString(),
                "for_team": team === "home",
            };

            console.log(data);

            socket.send(JSON.stringify(data));
        };

        element.playerClickHandler = playerClickHandler;
        element.addEventListener("click", playerClickHandler);
    });
};