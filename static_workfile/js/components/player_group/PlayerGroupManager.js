import { truncateMiddle } from '../../utils/index.js';

export class PlayerGroupManager {
    constructor(
        matchId,
        teamId,
        containerId = 'app-container',
        access = false,
        groupIdToTypeId = {},
        typeIdToGroupId = {},
    ) {
        // The one place we actually query the DOM: store a reference to our root container.
        this.container = document.getElementById(containerId);

        this.access = access;
        this.matchId = matchId;
        this.teamId = teamId;

        // Maintain an internal "virtual" representation or references
        this.selectedPlayers = [];
        this.selectedPlayersAdd = [];
        this.groupId = null;
        this.reserveId = '0192c7bb-c664-77fd-8a29-acde6f428c93';

        this.csrfToken = null;
        // We'll create the 'doneButton' dynamically instead of looking for it.
        this.doneButton = null;

        this.playerGroupsData = [];
        this.groupTypes = this.initializeGroupTypes();
        this.typeIds = [
            '01927d88-6878-7d0f-acbf-28c251fbc2b5',
            '01927d88-57ef-7339-a791-67cf856bfea1',
            '0192c7bb-c664-77fd-8a29-acde6f428c93',
        ];
        this.groupIdToTypeId = groupIdToTypeId;
        this.typeIdToGroupId = typeIdToGroupId;
        this.groupIdToStartingType = {};

        // Bind your methods for event usage
        this.boundSetupPlayerGroups = this.outsideSetupPlayerGroups.bind(this);

        // We'll keep a reference to our main content area in memory
        // (like your old 'player-field' container)
        this.playerField = null;
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
        // Example of still grabbing CSRF from the DOM (if needed).
        this.csrfToken =
            document.getElementsByName('csrfmiddlewaretoken')[0]?.value || '';

        // Now set up the initial UI
        this.setupPlayerGroups();

        if (Object.keys(this.groupIdToTypeId).length === 0) {
            this.mapGroupIdsToTypeIds();
        }
        if (Object.keys(this.typeIdToGroupId).length === 0) {
            this.mapTypeIdsToGroupIds();
        }
    }

    outsideSetupPlayerGroups() {
        this.setupPlayerGroups();
    }

    async setupPlayerGroups() {
        await this.fetchPlayersGroupsData();
        this.getPlayersGroupsData(); // creates or updates groupIdToStartingType
        // Re-render the main groups view
        this.renderPlayerGroupsView(this.playerGroupsData);
    }

    // -----------------------------
    // Rendering / UI Construction
    // -----------------------------

    /**
     * Fully renders the "player groups" view into this.playerField,
     * clears out the old content, and then appends it to our container.
     */
    renderPlayerGroupsView(playerGroups) {
        // 1) If we have an old playerField, remove it.
        if (this.playerField) {
            this.container.removeChild(this.playerField);
        }

        // 2) Create a new container for the groups.
        this.playerField = document.createElement('div');
        this.playerField.id = 'player-field';
        // Some styling classes if needed
        this.playerField.classList.add('scrollable');

        let nullPlayers = true;

        // For each group in the data, create its DOM content
        playerGroups.forEach((playerGroup) => {
            const isReserve = playerGroup.starting_type?.name === 'Reserve';
            const hasPlayers = playerGroup.players && playerGroup.players.length > 0;

            if (hasPlayers) {
                nullPlayers = false;

                // Group divider (title row)
                const groupDivider = this.createGroupDivider(playerGroup, isReserve);
                this.playerField.appendChild(groupDivider);

                // Divider line
                const dividerLine1 = document.createElement('hr');
                dividerLine1.classList.add('divider');
                this.playerField.appendChild(dividerLine1);

                // The group container
                const playerGroupDiv = document.createElement('div');
                playerGroupDiv.classList.add('flex-column', 'player-group');
                playerGroupDiv.id = playerGroup.id_uuid;

                // Create player rows
                playerGroup.players.forEach((player) => {
                    const { playerDiv, divider } = this.createPlayerRow(
                        player,
                        playerGroup.id_uuid,
                    );
                    playerGroupDiv.appendChild(playerDiv);
                    playerGroupDiv.appendChild(divider);
                });

                this.playerField.appendChild(playerGroupDiv);
            } else if (isReserve) {
                // The "Add players" area for an empty reserve group
                const centerDiv = document.createElement('div');
                centerDiv.classList.add('flex-center');
                if (nullPlayers) {
                    centerDiv.style.height = '100%';
                }
                centerDiv.style.width = '100%';

                if (this.access) {
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
                } else {
                    const noPlayers = document.createElement('p');
                    noPlayers.classList.add('dm-sans-400-normal');
                    noPlayers.textContent = 'Geen spelers beschikbaar';
                    centerDiv.appendChild(noPlayers);
                }
                this.playerField.appendChild(centerDiv);
            }
        });

        // If some players are already selected (edge case), ensure we show the options
        if (this.selectedPlayers.length > 0) {
            this.updateOptionsBar();
        }

        // 4) Attach new content to main container
        this.container.appendChild(this.playerField);
    }

    /**
     * Helper to create the group divider row with the group name
     * and possibly the "Speler toevoegen" button if it's a Reserve group.
     */
    createGroupDivider(playerGroup, isReserve) {
        const groupDivider = document.createElement('div');
        groupDivider.classList.add('flex-row', 'group-divider');

        const groupTitle = document.createElement('p');
        groupTitle.classList.add('dm-sans-600-normal');
        groupTitle.style.fontSize = '16px';
        groupTitle.style.margin = '0';
        groupTitle.textContent = playerGroup.starting_type.name;
        groupDivider.appendChild(groupTitle);

        if (isReserve && playerGroup.players.length > 0) {
            const addPlayerButton = document.createElement('p');
            addPlayerButton.classList.add('dm-sans-600-normal', 'done-button');
            addPlayerButton.style.marginLeft = 'auto';
            addPlayerButton.style.fontSize = '16px';
            addPlayerButton.style.margin = '0';
            addPlayerButton.textContent = 'Speler toevoegen';
            addPlayerButton.addEventListener('click', () => {
                this.fetchPlayersData();
            });
            groupDivider.appendChild(addPlayerButton);
        }

        return groupDivider;
    }

    /**
     * Helper to create a single player's row for display.
     * We attach the click handler directly here (no querySelector needed).
     */
    createPlayerRow(player, groupId) {
        const playerDiv = document.createElement('div');
        playerDiv.classList.add('flex-row', 'player');
        playerDiv.id = player.id_uuid;

        // If the player is in the "selectedPlayers" array, highlight
        const isSelected = this.selectedPlayers.some(
            (p) => p.id_uuid === player.id_uuid,
        );
        playerDiv.style.backgroundColor = isSelected ? 'lightblue' : 'white';

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

        // The line after the player row
        const dividerLine2 = document.createElement('hr');
        dividerLine2.classList.add('divider');

        // Attach the click event
        // Instead of referencing `parentElement.id`, we already know the groupId
        playerDiv.addEventListener('click', () => {
            this.handlePlayerSelection(player, groupId, playerDiv);
        });

        return { playerDiv, divider: dividerLine2 };
    }

    /**
     * Called by the click handler for each player. We do not query the DOM
     * to find groupId or anything; we receive them directly.
     */
    handlePlayerSelection(player, groupId, playerDiv) {
        // If the current selection is the same group or nothing is yet selected
        if (this.groupId === groupId || this.groupId === null) {
            this.togglePlayerSelection(player, groupId, playerDiv);
        }
    }

    togglePlayerSelection(player, groupId, playerDiv) {
        const id_uuid = player.id_uuid;
        const playerIndex = this.selectedPlayers.findIndex(
            (p) => p.id_uuid === id_uuid,
        );

        if (playerIndex === -1) {
            // Not selected yet, so add to selected
            this.selectedPlayers.push({ id_uuid, groupId });
            this.groupId = groupId;
        } else {
            // Already selected, so remove
            this.selectedPlayers.splice(playerIndex, 1);
            if (this.selectedPlayers.length === 0) {
                this.groupId = null;
            }
        }
        // Update highlight
        const isSelected = this.selectedPlayers.some((p) => p.id_uuid === id_uuid);
        playerDiv.style.backgroundColor = isSelected ? 'lightblue' : 'white';

        this.updateOptionsBar();
    }

    /**
     * Show or hide the options bar that appears when a group of players is selected.
     * Instead of searching for `#options-bar`, we keep an internal reference if needed.
     */
    updateOptionsBar() {
        if (this.selectedPlayers.length > 0) {
            if (!document.getElementById('options-bar')) {
                this.showOptionsBar();
            }
        } else {
            this.removeOptionsBar();
        }
    }

    showOptionsBar() {
        const optionsBar = document.createElement('div');
        optionsBar.classList.add('flex-row', 'options-bar');
        optionsBar.id = 'options-bar';

        const groupTypeId = this.groupIdToTypeId[this.groupId];
        const options = this.groupTypes[groupTypeId] || [];

        options.forEach((option) => {
            const button = document.createElement('button');
            button.innerText = option.text;
            button.addEventListener('click', () => this.handleOptionClick(option));
            optionsBar.appendChild(button);
        });

        // Append to the container we stored
        this.container.appendChild(optionsBar);
        this.adjustScrollableHeight('calc(100% - 48px)');
    }

    handleOptionClick(option) {
        const targetGroupId = this.typeIdToGroupId[option.id] || null;
        const playersGroupData = this.playerGroupsData.find(
            (group) => group.id_uuid === targetGroupId,
        );
        const numberPlayersGroup = playersGroupData?.players?.length || 0;

        // Check if we exceed maximum players for the group
        if (
            option.player_max &&
            numberPlayersGroup + this.selectedPlayers.length > option.player_max
        ) {
            alert(
                `You have selected too many players for the group.\nGroup max: ${option.player_max}.`,
            );
            return;
        }

        // Perform the action
        option.func(this.selectedPlayers, targetGroupId);
        this.removeOptionsBar();
    }

    removeOptionsBar() {
        const optionsBar = document.getElementById('options-bar');
        if (optionsBar) {
            this.container.removeChild(optionsBar);
            this.adjustScrollableHeight('');
        }
    }

    adjustScrollableHeight(height = '') {
        // Just tweak the style of our main "player-field" or container
        if (this.playerField) {
            this.playerField.style.height = height;
        }
    }

    // -----------------------------
    // Mapping Helpers
    // -----------------------------

    mapGroupIdsToTypeIds() {
        // You might still rely on actual DOM elements for mapping,
        // or, if you have them in data, use that. For now, we can skip or adapt:
        this.typeIds.forEach((typeId) => {
            // Suppose we had an object in memory that matched these IDs
            // Instead of document.getElementById(typeId), you can figure it out from data
            // This is just a placeholder or example
            const groupName = `GroupNameFor_${typeId}`;
            this.groupIdToTypeId[groupName] = typeId;
        });

        console.log('groupIdToTypeId: ', this.groupIdToTypeId);
    }

    mapTypeIdsToGroupIds() {
        this.typeIds.forEach((typeId) => {
            // Similarly handle in-memory data
            const groupName = `GroupNameFor_${typeId}`;
            this.typeIdToGroupId[typeId] = groupName;
        });

        console.log('typeIdToGroupId: ', this.typeIdToGroupId);
    }

    getPlayersGroupsData() {
        // Just set up groupIdToStartingType from the data
        this.playerGroupsData.forEach((group) => {
            this.groupIdToStartingType[group.id_uuid] = group.starting_type;
        });
    }

    // -----------------------------
    // Server Communication
    // -----------------------------

    async fetchPlayersGroupsData() {
        const data = await this.fetchData(
            `/match/api/player_overview_data/${this.matchId}/${this.teamId}/`,
        );
        if (data) {
            this.playerGroupsData = data.player_groups;
            // Build the mapping from group ID -> starting_type
            this.playerGroupsData.forEach((group) => {
                this.groupIdToStartingType[group.id_uuid] = group.starting_type;
            });
        }
    }

    async changePlayerGroup(selectedPlayers, newGroupId) {
        console.log('Changing group for players:', selectedPlayers, 'to:', newGroupId);
        if (newGroupId !== null) {
            newGroupId = newGroupId.trim();
        }

        await this.fetchData('/match/api/player_designation/', {
            players: selectedPlayers,
            new_group_id: newGroupId,
        });

        // Update local data
        this.updatePlayersGroupsData(selectedPlayers, newGroupId);

        // Clear selections
        this.selectedPlayers = [];
        this.groupId = null;
        this.updateOptionsBar();

        // Rerender
        this.renderPlayerGroupsView(this.playerGroupsData);
    }

    updatePlayersGroupsData(selectedPlayers, newGroupId) {
        selectedPlayers.forEach((selectedPlayer) => {
            const id_uuid = selectedPlayer.id_uuid;
            const oldGroupId = selectedPlayer.groupId;

            // Remove from old group
            const oldGroup = this.playerGroupsData.find(
                (g) => g.id_uuid === oldGroupId,
            );
            let player = null;
            if (oldGroup) {
                const idx = oldGroup.players.findIndex((p) => p.id_uuid === id_uuid);
                if (idx !== -1) {
                    [player] = oldGroup.players.splice(idx, 1);
                }
            }

            // Add to new group
            if (newGroupId && player) {
                let newGroup = this.playerGroupsData.find(
                    (g) => g.id_uuid === newGroupId,
                );
                if (!newGroup) {
                    const startingType = this.groupIdToStartingType[newGroupId] || {
                        name: 'Unknown',
                    };
                    newGroup = {
                        id_uuid: newGroupId,
                        starting_type: startingType,
                        players: [],
                    };
                    this.playerGroupsData.push(newGroup);
                    this.groupIdToStartingType[newGroupId] = startingType;
                }
                newGroup.players.push(player);
            }
        });
    }

    async fetchData(url, bodyData = null) {
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

    // -----------------------------
    // Adding Players Flow
    // -----------------------------

    async fetchPlayersData() {
        const data = await this.fetchData(
            `/match/api/players_team/${this.matchId}/${this.teamId}/`,
        );
        if (data) {
            this.renderAddPlayersView(data.players);
        }
    }

    async fetchSearchPlayers(value) {
        const data = await this.fetchData(
            `/match/api/player_search/${this.matchId}/${this.teamId}/?search=${value}`,
        );
        if (data) {
            this.renderAddPlayersView(data.players, value);
        }
    }

    /**
     * Renders the "Add Players" screen.
     * This replaces the main playerField content,
     * but we do not query .player or any elements for event handling.
     */
    renderAddPlayersView(players, searchText = null) {
        // Clear old content from the container if needed
        if (this.playerField) {
            this.container.removeChild(this.playerField);
        }

        // Scroll the container to the top
        this.container.scrollTop = 0;

        // Create a new container
        this.playerField = document.createElement('div');
        this.playerField.id = 'player-field';
        this.playerField.classList.add('scrollable');

        // Build the search area
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

        let searchTimeout = null;
        searchField.addEventListener('keyup', (event) => {
            clearTimeout(searchTimeout);

            if (event.key === 'Enter') {
                const playerName = searchField.value;
                if (playerName === '') {
                    this.fetchPlayersData();
                } else {
                    this.fetchSearchPlayers(searchField.value);
                }
            } else {
                // 2-second delay
                searchTimeout = setTimeout(() => {
                    const playerName = searchField.value;
                    if (playerName === '') {
                        this.fetchPlayersData();
                    } else {
                        this.fetchSearchPlayers(searchField.value);
                    }
                }, 2000);
            }
        });

        // Add icon
        const exitIcon = document.createElement('i');
        exitIcon.classList.add('fas', 'cross-icon');

        exitIcon.addEventListener('click', () => {
            this.removeOptionsBar();
            this.boundSetupPlayerGroups();
        });

        searchDiv.appendChild(exitIcon);

        this.playerField.appendChild(searchDiv);

        // Render list of players
        players.forEach((player) => {
            const { playerRow, divider } = this.createAddPlayerRow(player);
            this.playerField.appendChild(playerRow);
            this.playerField.appendChild(divider);
        });

        // Show any "selected but not in the fetched list" players
        const filteredSelected = this.selectedPlayersAdd.filter(
            (sel) => !players.some((p) => p.id_uuid === sel.id_uuid),
        );

        if (filteredSelected.length > 0) {
            const groupDivider = document.createElement('div');
            groupDivider.classList.add('flex-row', 'group-divider');

            const groupTitle = document.createElement('p');
            groupTitle.classList.add('dm-sans-600-normal');
            groupTitle.style.fontSize = '22px';
            groupTitle.style.margin = '0';
            groupTitle.textContent = 'Geselecteerde spelers';
            groupDivider.appendChild(groupTitle);

            this.playerField.appendChild(groupDivider);

            filteredSelected.forEach((player) => {
                const { playerRow, divider } = this.createAddPlayerRow(player, true);
                this.playerField.appendChild(playerRow);
                this.playerField.appendChild(divider);
            });
        }

        // Finally, attach
        this.container.appendChild(this.playerField);

        // Update the bottom bar if needed
        this.updateOptionsBarAddPlayers();
    }

    createAddPlayerRow(player, forceHighlight = false) {
        const playerRow = document.createElement('div');
        playerRow.classList.add('flex-row', 'player');
        playerRow.id = player.id_uuid;

        const isSelected =
            forceHighlight ||
            this.selectedPlayersAdd.some((p) => p.id_uuid === player.id_uuid);
        playerRow.style.backgroundColor = isSelected ? 'lightblue' : 'white';

        // Add profile picture
        const profileImg = document.createElement('img');
        profileImg.src = player.get_profile_picture;
        profileImg.alt = 'profile';
        profileImg.classList.add('profile_picture');
        playerRow.appendChild(profileImg);

        // Add username
        const username = document.createElement('p');
        username.classList.add('dm-sans-400-normal');
        username.style.marginLeft = '16px';
        username.textContent = truncateMiddle(player.user.username, 20);
        playerRow.appendChild(username);

        // Divider
        const dividerLine = document.createElement('hr');
        dividerLine.classList.add('divider');

        // Click handler
        playerRow.addEventListener('click', () => {
            this.handleSelectedPlayerClick(player, playerRow);
        });

        return { playerRow, divider: dividerLine };
    }

    handleSelectedPlayerClick(player, playerElement) {
        const alreadySelected = this.selectedPlayersAdd.some(
            (p) => p.id_uuid === player.id_uuid,
        );

        if (alreadySelected) {
            // remove from selected
            this.selectedPlayersAdd = this.selectedPlayersAdd.filter(
                (p) => p.id_uuid !== player.id_uuid,
            );
            playerElement.style.backgroundColor = 'white';
        } else {
            // add to selected
            this.selectedPlayersAdd.push(player);
            playerElement.style.backgroundColor = 'lightblue';
        }

        // Update the bottom bar
        this.updateOptionsBarAddPlayers();
    }

    updateOptionsBarAddPlayers() {
        if (this.selectedPlayersAdd.length === 0) {
            this.removeOptionsBar();
        } else if (!document.getElementById('options-bar')) {
            this.addPlayerOptionMenu();
        }
    }

    addPlayerOptionMenu() {
        const optionsBar = document.createElement('div');
        optionsBar.classList.add('flex-row', 'options-bar');
        optionsBar.id = 'options-bar';

        const addPlayerButton = document.createElement('button');
        addPlayerButton.innerText = 'Add player';
        addPlayerButton.addEventListener('click', () => this.handleAddPlayerClick());
        optionsBar.appendChild(addPlayerButton);

        this.container.appendChild(optionsBar);
        this.adjustScrollableHeight('calc(100% - 48px)');
    }

    async handleAddPlayerClick() {
        // we assume we want to add these players to the Reserve group
        const newGroupId = (this.typeIdToGroupId[this.reserveId] || '').trim();

        await this.fetchData('/match/api/player_designation/', {
            players: this.selectedPlayersAdd,
            new_group_id: newGroupId,
        });

        this.selectedPlayersAdd = [];
        this.boundSetupPlayerGroups();

        this.removeOptionsBar();
    }
}
