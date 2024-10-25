let selected_players = [];
let group_id = null;

window.addEventListener("DOMContentLoaded", function() {
    // get all the elements with the class 'player'
    const players = document.getElementsByClassName("player");

    for (const player of players) {
        player.addEventListener("click", function() {
            const group_id_local = player.parentElement ? player.parentElement.id : null;
            
            if (group_id == group_id_local || group_id == null) {
                if (!selected_players.includes(player.id)) {
                    const player_id = player.id;

                    selected_players.push(player_id);
                    group_id = group_id_local;

                    highlightSelectedPlayer(player, selected_players);
                } else {
                    const index = selected_players.indexOf(player.id);
                    selected_players.splice(index, 1);
                    if (selected_players.length === 0) {
                        group_id = null;
                    }

                    highlightSelectedPlayer(player, selected_players);
                }
            }
        });
    }
});

function highlightSelectedPlayer(player, selected_players) {
    if (selected_players.includes(player.id)) {
        player.style.backgroundColor = "lightblue";
    } else {
        player.style.backgroundColor = "white";
    }
}

function showOptionsBar() {
    const function_bar = document.getElementById("function_bar");
    function_bar.style.display = "block";
}