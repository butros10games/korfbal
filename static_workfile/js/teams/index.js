document.addEventListener("DOMContentLoaded", function() {
    setup_search();
});


function setup_search() {
    const searchContainer = document.querySelector('.search-container');
    const searchIcon = document.querySelector('.search-icon');
    const searchInput = document.querySelector('.search-input');

    searchContainer.addEventListener('click', () => {
        searchIcon.style.opacity = 0;
        searchInput.style.width = '150px';
        searchInput.style.opacity = 1;
        searchInput.focus();
    });

    searchInput.addEventListener('blur', () => {
        searchInput.style.width = '0';
        searchInput.style.opacity = 0;
        searchIcon.style.opacity = 1;
    });

    searchInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            const searchTerm = searchInput.value.trim();
            if (searchTerm !== '') {
                // Perform AJAX request here (replace the URL with your actual API endpoint)
                const apiUrl = `https://korfbal.butrosgroot.com/search/?q=${encodeURIComponent(searchTerm)}`;
                
                console.log(apiUrl);

                // Assuming you're using fetch API for the AJAX request
                fetch(apiUrl)
                    .then(response => response.json())
                    .then(data => {
                        // Handle the search results data
                        console.log(data);
                        displaySearchResults(data);
                    })
                    .catch(error => {
                        console.error('Error:', error);
                    });
            }
        }
    });
    
    function displaySearchResults(results) {
        teamsContainer = document.querySelector('.teams-container');

        teamsContainer.innerHTML = '';

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
}