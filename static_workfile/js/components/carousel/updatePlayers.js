import { truncateMiddle } from '../utils';
import { cleanDomCarousel } from './utils';

export const updatePlayers = function(data, infoContainer) {
    cleanDomCarousel(infoContainer);

    if (data.spelers.length > 0) {
        infoContainer.classList.add('flex-start-wrap');

        for (const element of data.spelers) {
            const player_container = document.createElement('a');
            player_container.href = element.get_absolute_url;
            player_container.style.textDecoration = 'none';
            player_container.style.color = '#000';
            player_container.classList.add('player-container');

            const player_profile_pic = document.createElement('img');
            player_profile_pic.classList.add('player-profile-pic');
            player_profile_pic.src = element.profile_picture;
            player_profile_pic.style.objectFit = 'cover';

            player_container.appendChild(player_profile_pic);

            const player_name = document.createElement('p');
            player_name.classList.add('player-name');
            player_name.style.fontSize = '16px';

            const PlayerName = truncateMiddle(element.name, 22);

            player_name.innerHTML = PlayerName;

            player_container.appendChild(player_name);

            infoContainer.appendChild(player_container);
        }
    } else {
        infoContainer.classList.add('flex-center');
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen spelers toegevoegd</p>";
    }
};
