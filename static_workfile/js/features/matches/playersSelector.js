import { truncateMiddle } from '../../utils/';

class PlayerGroupManager {
    constructor() {
        // Initialize properties
        this.selectedPlayers = [];
        this.selectedPlayersAdd = [];
        this.groupId = null;
        this.reserveId = '0192c7bb-c664-77fd-8a29-acde6f428c93';
        this.csrfToken = null;
        this.matchId = null;
        this.teamId = null;
        this.doneButton = null;
        this.playerGroupsData = [];
        this.groupTypes = this.initializeGroupTypes();
        this.typeIds = [
            '01927d88-6878-7d0f-acbf-28c251fbc2b5',
            '01927d88-57ef-7339-a791-67cf856bfea1',
            '0192c7bb-c664-77fd-8a29-acde6f428c93',
        ];
        this.groupIdToTypeId = {};
        this.typeIdToGroupId = {};
        this.groupIdToStartingType = {};

        this.boundLinkBack = this.linkBack.bind(this);
        this.boundsetupPlayerGroups = this.outsidesetupPlayerGroups.bind(this);
    }

    initializeGroupTypes() {
        // Define group types with corresponding actions
        const changeGroupBound = this.changePlayerGroup.bind(this);
        return {
            '01927d88-6878-7d0f-acbf-28c251fbc2b5': [
                {
                    text: 'Remove',
                    func: changeGroupBound,
                    id: '0192c7bb-c664-77fd-8a29-acde6f428c93',
                }, // Attack
            ],
            '01927d88-57ef-7339-a791-67cf856bfea1': [
                {
                    text: 'Remove',
                    func: changeGroupBound,
                    id: '0192c7bb-c664-77fd-8a29-acde6f428c93',
                }, // Defense
            ],
            '0192c7bb-c664-77fd-8a29-acde6f428c93': [
                { text: 'Remove', func: changeGroupBound },
                {
                    text: 'Attack',
                    func: changeGroupBound,
                    id: '01927d88-6878-7d0f-acbf-28c251fbc2b5',
                    player_max: 4,
                },
                {
                    text: 'Defense',
                    func: changeGroupBound,
                    id: '01927d88-57ef-7339-a791-67cf856bfea1',
                    player_max: 4,
                },
            ], // Reserve
        };
    }

    initialize() {
        // Set CSRF token and parse URL parameters
        this.csrfToken =
            document.getElementsByName('csrfmiddlewaretoken')[0]?.value || '';
        this.doneButton = document.getElementById('done-button');
        [this.matchId, this.teamId] = this.parseUrl();

        // Fetch initial data and set up the initial state
        this.setupPlayerGroups();
        this.mapGroupIdsToTypeIds();
        this.mapTypeIdsToGroupIds();
    }

    outsidesetupPlayerGroups() {
        this.setupPlayerGroups();
    }

    async setupPlayerGroups() {
        await this.fetchPlayersGroupsData();
        this.setupPlayerButtons();
        this.getPlayersGroupsData();
    }

    parseUrl() {
        // Extract matchId and teamId from the URL
        const urlParts = window.location.href.split('/');
        return [urlParts[urlParts.length - 3], urlParts[urlParts.length - 2]];
    }

    setupPlayerButtons() {
        // Add click event listeners to all player elements
        document.querySelectorAll('.player').forEach((player) => {
            player.addEventListener('click', () => this.handlePlayerSelection(player));
        });
    }

    handlePlayerSelection(player) {
        // Handle player selection and grouping logic
        const localGroupId = player.parentElement?.id || null;
        if (this.groupId === localGroupId || this.groupId === null) {
            this.togglePlayerSelection(player, localGroupId);
        }
    }

    togglePlayerSelection(player, localGroupId) {
        // Add or remove player from the selected list
        const id_uuid = player.id;
        const playerIndex = this.selectedPlayers.findIndex(
            (p) => p.id_uuid === id_uuid,
        );

        if (playerIndex === -1) {
            this.selectedPlayers.push({ id_uuid: id_uuid, groupId: localGroupId });
            this.groupId = localGroupId;
        } else {
            this.selectedPlayers.splice(playerIndex, 1);
            if (this.selectedPlayers.length === 0) {
                this.groupId = null;
            }
        }
        this.highlightSelectedPlayer(player);
        this.updateOptionsBar();
    }

    highlightSelectedPlayer(player) {
        // Highlight or unhighlight the selected player
        player.style.backgroundColor = this.selectedPlayers.some(
            (p) => p.id_uuid === player.id,
        )
            ? 'lightblue'
            : 'white';
    }

    updateOptionsBar() {
        // Show or hide the options bar based on selection
        if (this.selectedPlayers.length > 0) {
            if (!document.getElementById('options-bar')) {
                this.showOptionsBar();
            }
        } else {
            this.removeOptionsBar();
        }
    }

    showOptionsBar() {
        // Create and display the options bar with appropriate buttons
        const optionsBar = document.createElement('div');
        optionsBar.classList.add('flex-row', 'options-bar');
        optionsBar.id = 'options-bar';

        const options = this.groupTypes[this.groupIdToTypeId[this.groupId]] || [];
        options.forEach((option) => {
            const button = document.createElement('button');
            button.innerText = option.text;
            button.addEventListener('click', () => this.handleOptionClick(option));
            optionsBar.appendChild(button);
        });

        document.body.appendChild(optionsBar);
        this.adjustScrollableHeight('calc(100vh - 208px)');
    }

    handleOptionClick(option) {
        // Handle the action when an option button is clicked
        const targetGroupId = this.typeIdToGroupId[option.id] || null;
        const playersGroupData = this.playerGroupsData.find(
            (group) => group.id_uuid === targetGroupId,
        );
        const numberPlayersGroup = playersGroupData?.players?.length || 0;

        if (
            option.player_max &&
            numberPlayersGroup + this.selectedPlayers.length > option.player_max
        ) {
            alert(
                `You have selected too many players for the group.
                Group max: ${option.player_max}.`,
            );
            return;
        }

        option.func(this.selectedPlayers, targetGroupId);
        this.removeOptionsBar();
    }

    removeOptionsBar() {
        // Remove the options bar from the DOM
        const optionsBar = document.getElementById('options-bar');
        if (optionsBar) {
            optionsBar.remove();
            this.adjustScrollableHeight();
        }
    }

    adjustScrollableHeight(height = '') {
        // Adjust the height of the scrollable container
        const scrollableElement = document.querySelector('.scrollable');
        if (scrollableElement) {
            scrollableElement.style.height = height;
        }
    }

    mapGroupIdsToTypeIds() {
        this.typeIds.forEach((typeId) => {
            // We expect an element in the DOM with id == this typeId
            const groupElement = document.getElementById(typeId);
            if (groupElement) {
                this.groupIdToTypeId[groupElement.innerText.trim()] = typeId;
            }
        });

        console.log("groupIdToTypeId: ", this.groupIdToTypeId);
    }

    mapTypeIdsToGroupIds() {
        this.typeIds.forEach((typeId) => {
            const groupElement = document.getElementById(typeId);
            if (groupElement) {
                this.typeIdToGroupId[typeId] = groupElement.innerText.trim();
            }
        });

        console.log("typeIdToGroupId: ", this.typeIdToGroupId);
    }

    getPlayersGroupsData() {
        // Collect data about player groups from the stored data
        this.playerGroupsData.forEach((group) => {
            this.groupIdToStartingType[group.id_uuid] = group.starting_type;
        });
    }

    async fetchPlayersGroupsData() {
        // Fetch player groups data from the server
        const data = await this.fetchData(
            `/match/api/player_overview_data/${this.matchId}/${this.teamId}/`,
        );
        if (data) {
            this.playerGroupsData = data.player_groups; // Store the full data

            // Create mapping from group IDs to starting types
            this.playerGroupsData.forEach((group) => {
                this.groupIdToStartingType[group.id_uuid] = group.starting_type;
            });

            this.generatePlayerFieldHTML(this.playerGroupsData);
        }
    }

    async changePlayerGroup(selectedPlayers, newGroupId) {
        // Clean up newGroupId by removing leading and trailing whitespace and newline characters
        newGroupId = newGroupId.trim();
    
        // Send a request to change the player group
        await this.fetchData('/match/api/player_designation/', {
            players: selectedPlayers,
            new_group_id: newGroupId,
        });
    
        // Update the local data
        this.updatePlayersGroupsData(selectedPlayers, newGroupId);
    
        this.selectedPlayers = [];
    
        // Re-render the player field from the updated data
        this.generatePlayerFieldHTMLFromData();
    
        // Set up player buttons again
        this.setupPlayerButtons();
    
        // Reset selections
        this.groupId = null;
        this.updateOptionsBar();
    }

    updatePlayersGroupsData(selectedPlayers, newGroupId) {
        // Update the local data to reflect the moved players
        selectedPlayers.forEach((selectedPlayer) => {
            const id_uuid = selectedPlayer.id_uuid;
            const oldGroupId = selectedPlayer.groupId;

            // Remove player from old group
            const oldGroup = this.playerGroupsData.find(
                (group) => group.id_uuid === oldGroupId,
            );
            let player = null;
            if (oldGroup) {
                const playerIndex = oldGroup.players.findIndex(
                    (p) => p.id_uuid === id_uuid,
                );
                if (playerIndex !== -1) {
                    [player] = oldGroup.players.splice(playerIndex, 1);
                }
            }

            // Add player to new group
            if (newGroupId && player) {
                let newGroup = this.playerGroupsData.find(
                    (group) => group.id_uuid === newGroupId,
                );
                if (!newGroup) {
                    // Create new group if it doesn't exist
                    const startingType = this.groupIdToStartingType[newGroupId] || {
                        name: 'Unknown',
                    };
                    newGroup = {
                        id_uuid: newGroupId,
                        starting_type: startingType,
                        players: [],
                    };
                    this.playerGroupsData.push(newGroup);
                    // Update the mapping
                    this.groupIdToStartingType[newGroupId] = startingType;
                }
                newGroup.players.push(player);
            }
        });
    }

    generatePlayerFieldHTMLFromData() {
        this.generatePlayerFieldHTML(this.playerGroupsData);
    }

    async fetchData(url, bodyData = null) {
        // Fetch data from the server with optional POST data
        try {
            const response = await fetch(url, {
                method: bodyData ? 'POST' : 'GET',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': this.csrfToken,
                },
                body: bodyData ? JSON.stringify(bodyData) : null,
            });
            if (response.ok) {
                return await response.json();
            } else {
                throw new Error('Error fetching data.');
            }
        } catch (error) {
            console.error(error.message);
        }
    }

    generatePlayerFieldHTML(playerGroups) {
        // Generate the HTML structure for player groups
        const playerField = document.getElementById('player-field');
        if (!playerField) {
            return;
        }
        playerField.innerHTML = '';

        this.goToMatchOverview();

        let nullPlayers = true;

        playerGroups.forEach((playerGroup) => {
            if (playerGroup.players && playerGroup.players.length > 0) {
                nullPlayers = false;

                // Create group divider
                const groupDivider = document.createElement('div');
                groupDivider.classList.add('flex-row', 'group-divider');

                const groupTitle = document.createElement('p');
                groupTitle.classList.add('dm-sans-600-normal');
                groupTitle.style.fontSize = '22px';
                groupTitle.style.margin = '0';
                groupTitle.textContent = playerGroup.starting_type.name;

                groupDivider.appendChild(groupTitle);

                if (
                    playerGroup.starting_type.name === 'Reserve' &&
                    playerGroup.players.length > 0
                ) {
                    const addPlayerButton = document.createElement('p');
                    addPlayerButton.classList.add('dm-sans-600-normal', 'done-button');
                    addPlayerButton.style.marginLeft = 'auto';
                    addPlayerButton.style.fontSize = '18px';
                    addPlayerButton.style.margin = '0';
                    addPlayerButton.textContent = 'Speler toevoegen';
                    addPlayerButton.addEventListener('click', () => {
                        this.fetchPlayersData();
                    });

                    groupDivider.appendChild(addPlayerButton);
                }

                playerField.appendChild(groupDivider);

                // Create divider line
                const dividerLine1 = document.createElement('hr');
                dividerLine1.classList.add('divider');
                playerField.appendChild(dividerLine1);

                // Create player group container
                const playerGroupDiv = document.createElement('div');
                playerGroupDiv.id = playerGroup.id_uuid;
                playerGroupDiv.classList.add('flex-column', 'player-group');

                // Add players within the group
                playerGroup.players.forEach((player) => {
                    const playerDiv = document.createElement('div');
                    playerDiv.id = player.id_uuid;
                    playerDiv.classList.add('flex-row', 'player');
                    if (
                        this.selectedPlayers.some((p) => p.id_uuid === player.id_uuid)
                    ) {
                        playerDiv.style.backgroundColor = 'lightblue';
                    }

                    // Add profile picture
                    const profileImg = document.createElement('img');
                    profileImg.src = player.get_profile_picture;
                    profileImg.alt = 'profile';
                    profileImg.classList.add('profile_picture');
                    playerDiv.appendChild(profileImg);

                    // Add username
                    const username = document.createElement('p');
                    username.classList.add('dm-sans-400-normal');
                    username.style.marginLeft = '16px';
                    username.textContent = truncateMiddle(player.user.username, 20);
                    playerDiv.appendChild(username);

                    playerGroupDiv.appendChild(playerDiv);

                    // Add divider line after each player
                    const dividerLine2 = document.createElement('hr');
                    dividerLine2.classList.add('divider');
                    playerGroupDiv.appendChild(dividerLine2);
                });

                playerField.appendChild(playerGroupDiv);
            } else if (playerGroup.starting_type.name === 'Reserve') {
                // create a button to add a player to the reserve group
                const centerDiv = document.createElement('div');
                centerDiv.classList.add('flex-center');
                if (nullPlayers) {
                    centerDiv.style.height = '100%';
                }
                centerDiv.style.width = '100%';

                const addPlayerButton = document.createElement('p');
                addPlayerButton.classList.add(
                    'dm-sans-600-normal',
                    'flex-center',
                    'add-players-button',
                );
                addPlayerButton.textContent = 'Voeg spelers toe';
                addPlayerButton.addEventListener('click', () => {
                    this.fetchPlayersData();
                });

                centerDiv.appendChild(addPlayerButton);
                playerField.appendChild(centerDiv);
            }
        });

        if (this.selectedPlayers.length > 0) {
            this.updateOptionsBar();
        }
    }

    // request player data from the server
    async fetchPlayersData() {
        const data = await this.fetchData(
            `/match/api/players_team/${this.matchId}/${this.teamId}/`,
        );
        if (data) {
            console.log(data);

            this.generatePlayerAddFieldHTML(data.players);
        }
    }

    async fetchSearchPlayers(value) {
        const data = await this.fetchData(
            `/match/api/player_search/${this.matchId}/${this.teamId}/?search=${value}`,
        );
        if (data) {
            console.log(data);

            this.generatePlayerAddFieldHTML(data.players, value);
        }
    }

    generatePlayerAddFieldHTML(players, searchText = null) {
        // Generate the HTML structure for player groups
        const playerField = document.getElementById('player-field');
        if (!playerField) {
            return;
        }
        playerField.innerHTML = '';

        this.fetchPlayersGroupsDataHandler();

        // Add a search field to search for players not currently in the team
        const searchDiv = document.createElement('div');
        searchDiv.classList.add('flex-row', 'search-div');

        const searchField = document.createElement('input');
        searchField.type = 'text';
        searchField.placeholder = 'Zoek naar spelers';
        searchField.id = 'search-field';
        searchField.classList.add('search-field');
        if (searchText) {
            searchField.value = searchText;
        }
        searchDiv.appendChild(searchField);

        let searchTimeout;

        searchField.addEventListener('keyup', (event) => {
            // Clear the timeout if there’s any activity before 3 seconds
            clearTimeout(searchTimeout);

            // Trigger search immediately if Enter is pressed
            if (event.key === 'Enter') {
                const player_name = searchField.value;
                if (player_name === '') {
                    this.fetchPlayersData();
                } else {
                    this.fetchSearchPlayers(searchField.value);
                }
            } else {
                // Set a 3-second delay to start the search
                searchTimeout = setTimeout(() => {
                    const player_name = searchField.value;
                    if (player_name === '') {
                        this.fetchPlayersData();
                    } else {
                        this.fetchSearchPlayers(searchField.value);
                    }
                }, 2000);
            }
        });

        // add search icon
        const searchIcon = document.createElement('i');
        searchIcon.classList.add('fas', 'fa-search', 'search-icon');
        searchDiv.appendChild(searchIcon);

        playerField.appendChild(searchDiv);

        players.forEach((player) => {
            // Create player group container
            const playerGroupDiv = document.createElement('div');
            playerGroupDiv.id = player.id_uuid;
            playerGroupDiv.classList.add('flex-row', 'player');
            if (this.selectedPlayersAdd.some((p) => p.id_uuid === player.id_uuid)) {
                playerGroupDiv.style.backgroundColor = 'lightblue';
            }

            this.playerElementHTML(player, playerGroupDiv, playerField);
        });

        this.filterdSelectedPlayersAdd = this.selectedPlayersAdd.filter(
            (selectedPlayer) =>
                !players.some((player) => player.id_uuid === selectedPlayer.id_uuid),
        );

        if (this.filterdSelectedPlayersAdd.length > 0) {
            const groupDivider = document.createElement('div');
            groupDivider.classList.add('flex-row', 'group-divider');

            const groupTitle = document.createElement('p');
            groupTitle.classList.add('dm-sans-600-normal');
            groupTitle.style.fontSize = '22px';
            groupTitle.style.margin = '0';
            groupTitle.textContent = 'Geselecteerde spelers';

            groupDivider.appendChild(groupTitle);

            playerField.appendChild(groupDivider);
        }

        this.filterdSelectedPlayersAdd.forEach((player) => {
            // Create player group container
            const playerGroupDiv = document.createElement('div');
            playerGroupDiv.id = player.id_uuid;
            playerGroupDiv.classList.add('flex-row', 'player');
            playerGroupDiv.style.backgroundColor = 'lightblue';

            this.playerElementHTML(player, playerGroupDiv, playerField);
        });

        this.updateOptionsBarAddPlayers();
    }

    playerElementHTML(player, playerGroupDiv, playerField) {
        // Add profile picture
        const profileImg = document.createElement('img');
        profileImg.src = player.get_profile_picture;
        profileImg.alt = 'profile';
        profileImg.classList.add('profile_picture');
        playerGroupDiv.appendChild(profileImg);

        // Add username
        const username = document.createElement('p');
        username.classList.add('dm-sans-400-normal');
        username.style.marginLeft = '16px';
        username.textContent = truncateMiddle(player.user.username, 20);
        playerGroupDiv.appendChild(username);

        playerField.appendChild(playerGroupDiv);

        // Add divider line after each player
        const dividerLine2 = document.createElement('hr');
        dividerLine2.classList.add('divider');
        playerField.appendChild(dividerLine2);

        // Add click event listener to add player to the team
        playerGroupDiv.addEventListener('click', () =>
            this.handleSelectedPlayerClick(player),
        );
    }

    handleSelectedPlayerClick(player) {
        const playerElement = document.getElementById(player.id_uuid);
        if (playerElement.style.backgroundColor === 'lightblue') {
            playerElement.style.backgroundColor = 'white';
            this.selectedPlayersAdd = this.selectedPlayersAdd.filter(
                (p) => p.id_uuid !== player.id_uuid,
            );
        } else {
            playerElement.style.backgroundColor = 'lightblue';
            this.selectedPlayersAdd.push(player);
        }

        // Update the options bar display
        this.updateOptionsBarAddPlayers();
    }

    addPlayerOptionMenu() {
        // Create a dropdown menu with options for adding players
        const optionsBar = document.createElement('div');
        optionsBar.classList.add('flex-row', 'options-bar');
        optionsBar.id = 'options-bar';

        const addPlayerButton = document.createElement('button');
        addPlayerButton.innerText = 'Add player';
        addPlayerButton.addEventListener('click', () => this.handleAddPlayerClick());
        optionsBar.appendChild(addPlayerButton);

        document.body.appendChild(optionsBar);
        this.adjustScrollableHeight('calc(100vh - 208px)');
    }

    async handleAddPlayerClick() {
        // Clean up new_group_id by removing leading and trailing whitespace and newline characters
        const newGroupId = this.typeIdToGroupId[this.reserveId].trim();
    
        await this.fetchData('/match/api/player_designation/', {
            players: this.selectedPlayersAdd,
            new_group_id: newGroupId,
        });
    
        this.selectedPlayersAdd = [];
        this.boundsetupPlayerGroups();
    }

    updateOptionsBarAddPlayers() {
        if (this.selectedPlayersAdd.length === 0) {
            this.removeOptionsBar();
        } else if (!document.getElementById('options-bar')) {
            this.addPlayerOptionMenu();
        }
    }

    linkBack() {
        window.location.href = `/match/${this.matchId}/`;
    }

    // Function for first page (Match Overview)
    goToMatchOverview() {
        this.removeOptionsBar();

        this.doneButton.textContent = 'Wedstrijd overzicht';
        this.doneButton.removeEventListener('click', this.boundsetupPlayerGroups);
        this.doneButton.addEventListener('click', this.boundLinkBack);
    }

    // Function for second page (Fetch Players Groups Data)
    fetchPlayersGroupsDataHandler() {
        this.removeOptionsBar();

        this.doneButton.textContent = 'Speler groep';
        this.doneButton.removeEventListener('click', this.boundLinkBack);
        this.doneButton.addEventListener('click', this.boundsetupPlayerGroups);
    }
}

// Instantiate and initialize the class after DOM content is loaded
document.addEventListener('DOMContentLoaded', async () => {
    const playerGroupManager = new PlayerGroupManager();

    playerGroupManager.initialize();
});
