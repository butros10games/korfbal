document.addEventListener("DOMContentLoaded", function() {
    setup_search();
});


function setup_search() {
    const searchContainerFull = document.getElementById('search-container-full');
    const searchContainer = document.querySelector('.search-container');
    const searchIcon = document.querySelector('.search-icon');
    const searchInput = document.querySelector('.search-input');

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

    let searchTimer;

    function performSearch(searchTerm) {
        // Perform AJAX request here (replace the URL with your actual API endpoint)
        const apiUrl = `https://korfbal.butrosgroot.com/search/?q=${encodeURIComponent(searchTerm)}`;

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
                    requestNormalView()
                });

                pastRequest.appendChild(removeButton);

                searchContainerFull.prepend(pastRequest);
            })
            .catch(error => {
                console.error('Error:', error);
            });
    };

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
    
    function displaySearchResults(results) {
        teamsContainer = document.querySelector('.teams-container');

        teamsContainer.innerHTML = '';

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

    function requestNormalView() {
        const apiUrl = `https://korfbal.butrosgroot.com/teams/indexdata/`;

        // Assuming you're using fetch API for the AJAX request
        fetch(apiUrl)
            .then(response => response.json())
            .then(data => {
                console.log(data);
                displayNormalIndex(data);
            })
            .catch(error => {
                console.error('Error:', error);
            });
    }

    function displayNormalIndex(data) {
        teamsContainer = document.querySelector('.teams-container');

        teamsContainer.innerHTML = '';

        followingText = document.createElement('p');
        followingText.innerHTML = 'Playing';
        followingText.style.fontWeight = '600';
        followingText.style.marginBottom = '8px';

        teamsContainer.appendChild(followingText);

        data.teams.forEach(element => {
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

        followingText = document.createElement('p');
        followingText.innerHTML = 'Following';
        followingText.style.fontWeight = '600';
        followingText.style.marginBottom = '8px';

        teamsContainer.appendChild(followingText);

        data.following_teams.forEach(element => {
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
}