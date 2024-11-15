"use strict";

let socket;
let player_id;
let WebSocket_url;
let csrfToken;
const profilePicture = document.getElementById("profilePic-container");
const infoContainer = document.getElementById("info-container");
const carousel = document.querySelector('.carousel');
const buttons = document.querySelectorAll('.button');

const regex = /([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/;
const url = window.location.href;
const maxLength = 14;

window.addEventListener("DOMContentLoaded", function() {
    csrfToken = document.querySelector('input[name="csrfmiddlewaretoken"]').value;

    const matches = regex.exec(url);
    if (matches) {
        player_id = matches[1];
        console.log(player_id);
    } else {
        console.log("No UUID found in the URL.");
    }

    WebSocket_url = "wss://" + window.location.host + "/ws/profile/" + player_id + "/";
    socket = initializeSocket(WebSocket_url, onMessageReceived);

    socket.onopen = function() {
        console.log("WebSocket connection established, sending initial data...");
        requestInitalData(".button.active", socket);
    };

    setupCarousel(carousel, buttons, socket);
    setupProfilePicture();
});

function setupProfilePicture() {
    const imageModal = document.getElementById('imageModal');
    const closeModalButton = document.getElementById('closeModal');

    // Show modal when an image is selected
    const fileInput = document.getElementById('profilePicInput');
    const imagePreview = document.getElementById('imagePreview');
    if (fileInput) {
        fileInput.addEventListener('change', async function() {
            const file = fileInput.files[0];
            if (file) {
                let blob = file;
        
                // Check if the file is a HEIC file
                if (file.name.toLowerCase().endsWith('.heic')) {
                    try {
                        // Convert HEIC to JPEG
                        blob = await heic2any({
                            blob: file,
                            toType: "image/jpeg",
                            quality: 0.7 // Adjust quality as needed
                        });
                    } catch (error) {
                        console.error('Error converting HEIC to JPEG:', error);
                        return;
                    }
                }
        
                const reader = new FileReader();
                reader.onload = function(e) {
                    imagePreview.src = e.target.result;
                    imageModal.style.display = 'flex'; // Show the modal
                };
                reader.readAsDataURL(blob);
            }
        });
    }

    // Close modal when the close button is clicked
    if (closeModalButton) {
        closeModalButton.addEventListener('click', function() {
            imageModal.style.display = 'none'; // Hide the modal
        });
    }

    // Add event listener for save button
    const saveButton = document.getElementById('saveProfilePic');
    if (saveButton) {
        saveButton.addEventListener('click', function() {
            const file = fileInput.files[0];
            if (file) {
                const blob = file;
        
                // Convert HEIC to JPEG before upload if necessary
                if (file.name.toLowerCase().endsWith('.heic')) {
                    heic2any({
                        blob: file,
                        toType: "image/jpeg",
                        quality: 0.7
                    })
                    .then(convertedBlob => {
                        uploadImage(convertedBlob);
                    })
                    .catch(error => {
                        console.error('Error converting HEIC to JPEG:', error);
                    });
                } else {
                    uploadImage(blob);
                }
            }
        });
    }
}

function uploadImage(blob) {
    const formData = new FormData();
    formData.append('profile_picture', blob, 'profile_picture.jpg'); // Set a default filename for the JPEG

    fetch('/profile/api/upload_profile_picture/', {
        method: 'POST',
        body: formData,
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
    .then(response => response.json())
    .then(data => {
        imageModal.style.display = 'none'; // Hide the modal
        document.getElementById('profilePic').src = '/media' + data.url;
    })
    .catch(error => {
        console.error('Error:', error);
    });
}

function onMessageReceived(event) {
    const data = JSON.parse(event.data);
    console.log(data);

    switch(data.command) {
        case "settings_request": {
            cleanDom();

            updateSettings(data);
            break;
        }
        
        case "player_goal_stats": {
            cleanDom();

            updateGoalStats(data);
            break;
        }

        case "settings_updated": {
            const saveButtonText = document.getElementById("save-button-text");
            saveButtonText.innerHTML = "Saved!";
            saveButtonText.style.color = "#fff";

            const saveButton = document.querySelector(".save-button");
            saveButton.classList.remove("loading");

            setTimeout(() => {
                saveButtonText.innerHTML = "Save";
                saveButtonText.style.color = "";
            }, 1500);
            break;
        }

        case "teams": {
            cleanDom();

            updateTeam(data, infoContainer); // imported from common/updateTeam.js
            break;
        }

        case "matches": {
            cleanDom();

            updateMatches(data, maxLength, infoContainer); // imported from common/updateMatches.js
            break;
        }
    }
}

function cleanDom() {
    infoContainer.innerHTML = "";
    infoContainer.classList.remove("flex-center");
    infoContainer.classList.remove("flex-start-wrap");

    profilePicture.classList.remove("active-img");
}

function updateSettings(data) {
    profilePicture.classList.add("active-img");

    // Main container for settings
    const settingsContainer = document.createElement("div");
    settingsContainer.classList.add("flex-column");

    const settingsRow = document.createElement("div");
    settingsRow.classList.add("flex-row");

    const settingsText = document.createElement("div");
    settingsText.classList.add("from-container");

    // Create input field for each setting
    const fields = [
        { name: "username", label: "Username", type: "text" },
        { name: "email", label: "Email", type: "email" },
        { name: "first_name", label: "First Name", type: "text" },
        { name: "last_name", label: "Last Name", type: "text" },
        { name: "email_2fa", label: "2FA Enabled", type: "checkbox" }
    ];

    fields.forEach(field => {
        const inputShell = document.createElement("div");

        if (field.type === "checkbox") {
            inputShell.classList.add("text-checkbox-shell");
        } else {
            inputShell.classList.add("text-input-shell");
        }

        const label = document.createElement("label");
        label.for = field.name;
        label.innerHTML = field.label;

        inputShell.appendChild(label);

        let input;
        if (field.type === "checkbox") {
            input = document.createElement("input");
            input.type = "checkbox";
            input.checked = data[field.name] ? data[field.name] : false;
            input.classList.add("text-checkbox");
        } else {
            input = document.createElement("input");
            input.type = field.type;
            input.value = data[field.name] && data[field.name].trim() !== "" ? data[field.name] : "";
            input.placeholder = field.label + "...";
            input.classList.add("text-input");
        }
        input.id = field.name;
        input.name = field.name;

        inputShell.appendChild(input);
        settingsText.appendChild(inputShell);
    });

    const saveButton = document.createElement("button");
    saveButton.type = "submit";
    
    const saveButtonText = document.createElement("p");
    saveButtonText.innerHTML = "Save";
    saveButtonText.style.margin = "0";
    saveButtonText.id = "save-button-text";

    saveButton.appendChild(saveButtonText);

    saveButton.classList.add("save-button");

    saveButton.addEventListener("click", function(event) {
        event.preventDefault(); // Prevent form submission

        saveButton.classList.add("loading");

        // Gather input values
        const formData = {
            'username': document.getElementById("username").value,
            'email': document.getElementById("email").value,
            'first_name': document.getElementById("first_name").value,
            'last_name': document.getElementById("last_name").value,
            'email_2fa': document.getElementById("email_2fa").checked
        };

        // Send data to server
        socket.send(JSON.stringify({
            'command': 'settings_update',
            'data': formData
        }));
    });

    settingsText.appendChild(saveButton);
    
    // django logout button
    const logoutButton = document.createElement("a");
    logoutButton.href = "/logout";
    logoutButton.innerHTML = "Logout";
    logoutButton.classList.add("logout-button");

    settingsText.appendChild(logoutButton);

    settingsRow.appendChild(settingsText);
    settingsContainer.appendChild(settingsRow);

    // Append the settingsContainer to the main container (assuming it's named infoContainer)
    infoContainer.innerHTML = ""; // Clear existing content
    infoContainer.appendChild(settingsContainer);
}

function updateGoalStats(data) {
    if (data.played_matches > 0) {
        const goals_container = document.createElement("div");
        goals_container.classList.add("flex-column");
        goals_container.style.width = "calc(100% - 24px))";
        goals_container.style.padding = "12px";

        const row_1 = document.createElement("div");
        row_1.classList.add("flex-row");
        row_1.style.justifyContent = "space-around";
        row_1.style.width = "100%";
        row_1.style.marginBottom = "24px";
        
        const matchs_container = document.createElement("div");
        matchs_container.classList.add("flex-column");
        matchs_container.style.width = "144px";

        const matchs = document.createElement("p");
        matchs.style.margin = "0";
        matchs.style.fontSize = "16px";
        matchs.innerHTML = "Wedstrijden";

        const matchs_data = document.createElement("p");
        matchs_data.style.margin = "0";
        matchs_data.innerHTML = data.played_matches;

        matchs_container.appendChild(matchs);
        matchs_container.appendChild(matchs_data);

        row_1.appendChild(matchs_container);

        const total_score_container = document.createElement("div");
        total_score_container.classList.add("flex-column");
        total_score_container.style.width = "144px";

        const total_score = document.createElement("p");
        total_score.style.margin = "0";
        total_score.style.fontSize = "16px";
        total_score.innerHTML = "Totaal punten";

        const total_score_data = document.createElement("p");
        total_score_data.style.margin = "0";
        total_score_data.innerHTML = data.total_goals_for + '/' + data.total_goals_against;

        total_score_container.appendChild(total_score);
        total_score_container.appendChild(total_score_data);

        row_1.appendChild(total_score_container);

        goals_container.appendChild(row_1);

        // Create a container for goal stats per type
        const goal_stats_container = document.createElement("div");
        goal_stats_container.classList.add("flex-row");
        goal_stats_container.style.width = "100%";
        goal_stats_container.style.marginTop = "12px";
        goal_stats_container.style.flexWrap = "wrap";
        goal_stats_container.style.justifyContent = "space-around";

        // Iterate through goal_stats object
        for (const goalType in data.player_goal_stats) {
            if (data.player_goal_stats.hasOwnProperty(goalType)) {
                const goalStat = data.player_goal_stats[goalType];

                // Create a div for each goal type's stats
                const goal_type_container = document.createElement("div");
                goal_type_container.classList.add("flex-column");
                goal_type_container.style.marginbottom = "12px";
                goal_type_container.style.width = "104px";
                goal_type_container.style.marginBottom = "12px";

                const goal_type_name = document.createElement("p");
                goal_type_name.style.margin = "0";
                goal_type_name.style.fontSize = "16px";
                goal_type_name.innerHTML = goalType;

                const goals_data = document.createElement("p");
                goals_data.style.margin = "0";
                goals_data.innerHTML = goalStat.goals_by_player + "/" + goalStat.goals_against_player;

                goal_type_container.appendChild(goal_type_name);
                goal_type_container.appendChild(goals_data);

                goal_stats_container.appendChild(goal_type_container);
            }
        }

        goals_container.appendChild(goal_stats_container);
        infoContainer.appendChild(goals_container);
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen doelpunten gemaakt</p>";
    }
}
