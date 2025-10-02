let selectedValue;
let oldSearchTerm = '';
let searchTimer;
let isSearching = false;

let searchContainerFull;
let searchContainer;
let searchIcon;
let searchInput;

let teamsContainer;

let csrfToken;

document.addEventListener('DOMContentLoaded', () => {
    teamsContainer = document.querySelector('.teams-container');
    csrfToken = document.querySelector('input[name="csrfmiddlewaretoken"]').value;

    setupSearch();
    setupTypeSelection();
});

function loadIcon() {
    teamsContainer.classList.add('flex-center');
    teamsContainer.innerHTML =
        "<div id='load_icon' class='lds-ring'><div></div><div></div><div></div><div></div></div>";
}

function cleanDom() {
    teamsContainer.innerHTML = '';
    teamsContainer.classList.remove('flex-center', 'flex-start-wrap');
}

function setupTypeSelection() {
    const typeElement = document.getElementById('type');
    typeElement.addEventListener('change', handleTypeChange);
    selectedValue = typeElement.value;
    ajaxRequestIndex(selectedValue);
}

function handleTypeChange() {
    selectedValue = this.value;
    console.log(selectedValue);

    if (oldSearchTerm === '') {
        ajaxRequestIndex(selectedValue);
    } else {
        performSearch(oldSearchTerm);
    }
}

function ajaxRequestIndex(value) {
    cleanDom();
    loadIcon();

    makeFetchRequest('/api/catalog/data', { value: value })
        .then((data) => displayNormalIndex(data))
        .catch((error) => console.error('Error:', error));
}

function setupSearch() {
    searchContainerFull = document.getElementById('search-container-full');
    searchContainer = document.querySelector('.search-container');
    searchIcon = document.querySelector('.search-icon');
    searchInput = document.querySelector('.search-input');

    searchContainer.addEventListener('click', expandSearchInput);
    searchInput.addEventListener('blur', collapseSearchInput);
    searchInput.addEventListener('keyup', handleSearchInput);
}

function expandSearchInput() {
    searchIcon.style.width = '0';
    searchIcon.style.opacity = 0;
    searchInput.style.width = '128px';
    searchInput.style.opacity = 1;
    searchInput.style.paddingLeft = '4px';
    searchInput.focus();
}

function collapseSearchInput() {
    searchInput.style.width = '0';
    searchInput.style.opacity = 0;
    searchInput.style.paddingLeft = '0';
    searchIcon.style.opacity = 1;
    searchIcon.style.width = '24px';
}

function handleSearchInput(event) {
    const searchTerm = searchInput.value.trim();
    if (event.key === 'Enter') {
        event.preventDefault();
        triggerSearch(searchTerm);
    } else if (searchTerm !== '') {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => triggerSearch(searchTerm), 500);
    }
}

function triggerSearch(searchTerm) {
    if (searchTerm !== '' && !isSearching) {
        isSearching = true;
        performSearch(searchTerm).finally(() => {
            isSearching = false;
        });
    }
}

function performSearch(searchTerm) {
    cleanDom();
    loadIcon();
    oldSearchTerm = searchTerm;

    const apiUrl = `https://${globalThis.location.host}/api/search/?q=${encodeURIComponent(searchTerm)}&category=${encodeURIComponent(selectedValue)}`;
    return makeFetchRequest(apiUrl)
        .then((data) => {
            displaySearchResults(data.results);
            searchInput.value = '';
            addPastRequest(searchTerm);
        })
        .catch((error) => console.error('Error:', error));
}

function makeFetchRequest(url, bodyData = null) {
    const options = {
        method: bodyData ? 'POST' : 'GET',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken,
        },
    };
    if (bodyData) {
        options.body = JSON.stringify(bodyData);
    }

    return fetch(url, options).then((response) => response.json());
}

function addPastRequest(searchTerm) {
    const previousRequests = document.querySelectorAll('.past-request');
    previousRequests.forEach((element) => element.remove());

    const pastRequest = document.createElement('div');
    pastRequest.classList.add('past-request');

    const pastRequestText = document.createElement('p');
    pastRequestText.innerHTML = searchTerm;
    pastRequestText.classList.add('past-request-text');
    pastRequest.appendChild(pastRequestText);

    const removeButton = document.createElement('p');
    removeButton.style.margin = 0;
    removeButton.innerHTML = 'x';
    removeButton.classList.add('remove-button');
    removeButton.addEventListener('click', () => {
        pastRequest.remove();
        oldSearchTerm = '';
        ajaxRequestIndex(selectedValue);
    });

    pastRequest.appendChild(removeButton);
    searchContainerFull.prepend(pastRequest);
}

function displaySearchResults(results) {
    cleanDom();

    if (results.length === 0) {
        teamsContainer.innerHTML = '<p>No results found ):</p>';
        return;
    }

    results.forEach((element) => {
        teamsContainer.appendChild(createTeamButton(element));
    });
}

function displayNormalIndex(data) {
    cleanDom();
    const buttonDiv = createButtonDiv(['Aangesloten', 'Volgend']);
    teamsContainer.appendChild(buttonDiv);

    const connectedDiv = createTeamListDiv(
        'Aangesloten',
        data.connected,
        'You are not playing in any teams yet ):',
    );
    const followingDiv = createTeamListDiv(
        'Volgend',
        data.following,
        'You are not following any teams yet ):',
    );
    followingDiv.style.display = 'none';

    teamsContainer.appendChild(connectedDiv);
    teamsContainer.appendChild(followingDiv);
}

function createButtonDiv(buttonArray) {
    const buttonDiv = document.createElement('div');
    buttonDiv.classList.add('flex-row');

    buttonArray.forEach((button) => {
        const buttonElement = document.createElement('div');
        buttonElement.classList.add('selection-button', 'flex-center');
        if (button === 'Aangesloten') {
            buttonElement.classList.add('active');
        }
        buttonElement.innerHTML = button;
        buttonElement.addEventListener('click', () =>
            handleButtonClick(buttonElement, button),
        );
        buttonDiv.appendChild(buttonElement);
    });

    return buttonDiv;
}

function handleButtonClick(buttonElement, button) {
    const buttons = document.querySelectorAll('.selection-button');
    buttons.forEach((buttonSelect) => buttonSelect.classList.remove('active'));
    buttonElement.classList.add('active');

    document.getElementById(button).style.display = 'flex';
    document.getElementById(
        button === 'Aangesloten' ? 'Volgend' : 'Aangesloten',
    ).style.display = 'none';
}

function createTeamListDiv(id, teams, emptyMessage) {
    const div = document.createElement('div');
    div.id = id;
    div.classList.add('flex-column');

    if (teams.length > 0) {
        teams.forEach((team) => div.appendChild(createTeamButton(team)));
    } else {
        const emptyText = document.createElement('p');
        emptyText.innerHTML = emptyMessage;
        emptyText.style.marginBottom = '8px';
        div.appendChild(emptyText);
    }

    return div;
}

function createTeamButton(element) {
    const teamButton = document.createElement('a');
    teamButton.classList.add('flex-row', 'team-button');
    teamButton.href = element.url;

    const logo = document.createElement('img');
    logo.src = element.img_url;
    logo.classList.add('team-logo');
    teamButton.appendChild(logo);

    const textContainer = document.createElement('div');
    textContainer.classList.add('flex-column', 'team-container');

    const teamName = document.createElement('p');
    teamName.style.fontWeight = '600';
    teamName.style.margin = 0;
    teamName.style.fontSize = '18px';
    teamName.innerHTML = element.name;
    textContainer.appendChild(teamName);

    if (element.competition) {
        const competition = document.createElement('p');
        competition.style.margin = 0;
        competition.style.fontSize = '14px';
        competition.style.color = '#666';
        competition.style.fontWeight = '500';
        competition.style.marginTop = '4px';
        competition.innerHTML = element.competition;
        textContainer.appendChild(competition);
    }

    teamButton.appendChild(textContainer);
    return teamButton;
}
