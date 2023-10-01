let selectedValue
let oldSearchTerm = ''
let searchTimer;

let searchContainerFull;
let searchContainer;
let searchIcon;
let searchInput;

let teamsContainer;

document.addEventListener("DOMContentLoaded", function() {
    teamsContainer = document.querySelector('.teams-container');

    setup_search();

    stypeSelection()
});

function load_icon() {
    teamsContainer.classList.add("flex-center");
    teamsContainer.innerHTML = "<div id='load_icon' class='lds-ring'><div></div><div></div><div></div><div></div></div>";
}

function cleanDom() {
    teamsContainer.innerHTML = "";
    teamsContainer.classList.remove("flex-center");
    teamsContainer.classList.remove("flex-start-wrap");
}

function stypeSelection() {
    document.getElementById('type').addEventListener('change', function() {
        // Get the selected value
        selectedValue = this.value;
        console.log(selectedValue);

        if (oldSearchTerm == '') {
            ajaxRequestIndex(selectedValue)
        } else {
            performSearch(oldSearchTerm)
        }
    });

    selectedValue = document.getElementById('type').value;
    ajaxRequestIndex(selectedValue)
}

function ajaxRequestIndex(value) {
    cleanDom()
    load_icon()
    
    // Make an AJAX request using the Fetch API
    fetch('/teams/indexdata/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ value: value })
    })
    .then(response => response.json())
    .then(data => {
        console.log('Success:', data);
        displayNormalIndex(data);
    })
    .catch(error => {
        console.error('Error:', error);
    });
}

function setup_search() {
    searchContainerFull = document.getElementById('search-container-full');
    searchContainer = document.querySelector('.search-container');
    searchIcon = document.querySelector('.search-icon');
    searchInput = document.querySelector('.search-input');

    searchContainer.addEventListener('click', () => {
        searchIcon.style.width = '0';
        searchIcon.style.opacity = 0;
        searchInput.style.width = '128px';
        searchInput.style.opacity = 1;
        searchInput.style.paddingLeft = '4px';
        searchInput.focus();
    });

    searchInput.addEventListener('blur', () => {
        searchInput.style.width = '0';
        searchInput.style.opacity = 0;
        searchInput.style.paddingLeft = '0';
        searchIcon.style.opacity = 1;
        searchIcon.style.width = '24px';
    });

    searchInput.addEventListener('keyup', (event) => {
        const searchTerm = searchInput.value.trim();
        if (event.key === 'Enter') {
            // Prevent the default behavior of the Enter key
            event.preventDefault();
    
            if (searchTerm !== '') {
                // Perform the search immediately for Enter key presses
                performSearch(searchTerm);
            }
        } else {
            // For other key presses (not Enter), trigger the search after a brief delay
            if (searchTerm !== '') {
                clearTimeout(searchTimer);

                searchTimer = setTimeout(() => {
                    // Call the performSearch function with the current search term
                    performSearch(searchTerm);
                }, 500);
            }
        }    
    });
}

function performSearch(searchTerm) {
    cleanDom()
    load_icon()

    oldSearchTerm = searchTerm 
    // Perform AJAX request here (replace the URL with your actual API endpoint)
    const apiUrl = `https://korfbal.butrosgroot.com/search/?q=${encodeURIComponent(searchTerm)}&category=${encodeURIComponent(selectedValue)}`;

    // Assuming you're using fetch API for the AJAX request
    fetch(apiUrl)
        .then(response => response.json())
        .then(data => {
            // Handle the search results data
            console.log(data);
            displaySearchResults(data);
            searchInput.value = '';

            previusRequests = document.querySelectorAll('.past-request');
            previusRequests.forEach(element => {
                element.remove();
            });

            pastRequest = document.createElement('div');
            pastRequest.classList.add('past-request');

            pastRequestText = document.createElement('p');
            pastRequestText.innerHTML = searchTerm;
            pastRequestText.classList.add('past-request-text');

            pastRequest.appendChild(pastRequestText);

            removeButton = document.createElement('p');
            removeButton.style.margin = 0;
            removeButton.innerHTML = 'x';
            removeButton.classList.add('remove-button');

            removeButton.addEventListener('click', () => {
                pastRequest.remove();
                oldSearchTerm = '';
                ajaxRequestIndex(selectedValue)
            });

            pastRequest.appendChild(removeButton);

            searchContainerFull.prepend(pastRequest);
        })
        .catch(error => {
            console.error('Error:', error);
        });
};
    
function displaySearchResults(results) {
    cleanDom()

    if (results.teams.length === 0) {
        teamsContainer.innerHTML = '<p>No results found ):</p>';
        return;
    }

    results.teams.forEach(element => {
        team_button = document.createElement('a');
        team_button.classList.add('flex-column', 'team-button');
        team_button.href = element.url;

        divContainer = document.createElement('div');
        divContainer.classList.add('flex-row', 'team-container');

        team_name = document.createElement('p');
        team_name.style.fontWeight = '600';
        team_name.innerHTML = element.name;

        divContainer.appendChild(team_name);
        team_button.appendChild(divContainer);
        teamsContainer.appendChild(team_button);
    });
}

function displayNormalIndex(data) {
    cleanDom()

    followingText = document.createElement('p');
    followingText.innerHTML = 'Playing';
    followingText.style.fontWeight = '600';
    followingText.style.marginBottom = '8px';
    followingText.style.marginTop = '4px';

    teamsContainer.appendChild(followingText);

    if (data.connected.length > 0) {
        data.connected.forEach(element => {
            team_button = document.createElement('a');
            team_button.classList.add('flex-column', 'team-button');
            team_button.href = element.url;

            divContainer = document.createElement('div');
            divContainer.classList.add('flex-row', 'team-container');

            team_name = document.createElement('p');
            team_name.style.fontWeight = '600';
            team_name.innerHTML = element.name;

            divContainer.appendChild(team_name);
            team_button.appendChild(divContainer);
            teamsContainer.appendChild(team_button);
        });
    } else {
        followingText = document.createElement('p');
        followingText.innerHTML = 'You are not playing in any teams yet ):';
        followingText.style.marginBottom = '8px';

        teamsContainer.appendChild(followingText);
    }


    followingText = document.createElement('p');
    followingText.innerHTML = 'Following';
    followingText.style.fontWeight = '600';
    followingText.style.marginBottom = '8px';

    teamsContainer.appendChild(followingText);

    if (data.following.length > 0) {
        data.following.forEach(element => {
            team_button = document.createElement('a');
            team_button.classList.add('flex-column', 'team-button');
            team_button.href = element.url;

            divContainer = document.createElement('div');
            divContainer.classList.add('flex-row', 'team-container');

            team_name = document.createElement('p');
            team_name.style.fontWeight = '600';
            team_name.innerHTML = element.name;

            divContainer.appendChild(team_name);
            team_button.appendChild(divContainer);
            teamsContainer.appendChild(team_button);
        });
    } else {
        followingText = document.createElement('p');
        followingText.innerHTML = 'You are not following any teams yet ):';
        followingText.style.marginBottom = '8px';

        teamsContainer.appendChild(followingText);
    }
}