import { cleanDomCarousel } from "./utils";

export const updateTeam = function(data, infoContainer) {
    cleanDomCarousel(infoContainer);

    if (data.teams.length > 0) {
        for (const element of data.teams) {
            const team_container = document.createElement("a");
            team_container.classList.add("flex-row");
            team_container.style.justifyContent = "flex-start";
            team_container.style.padding = "12px";
            team_container.style.borderBottom = "1px solid rgb(0 0 0 / 20%)";
            team_container.style.width = "calc(100% - 24px)";
            team_container.style.textDecoration = "none";
            team_container.style.color = "#000";
            team_container.href = element.get_absolute_url;

            const team_picture = document.createElement("img");
            team_picture.style.objectFit = "contain";
            team_picture.src = element.logo;
            team_picture.style.width = "48px";
            team_picture.style.height = "48px";

            const team_name = document.createElement("p");
            team_name.style.margin = "12px 6px";
            team_name.style.fontSize = "16px";
            team_name.innerHTML = element.name;

            const arrow_div = document.createElement("div");
            arrow_div.classList.add("flex-center");
            arrow_div.style.width = "24px";
            arrow_div.style.height = "24px";
            arrow_div.style.marginLeft = "auto";

            const arrow = document.createElement("img");
            arrow.src = `https://static.${window.location.origin}/images/arrow.svg`;
            arrow.style.width = "18px";
            // rotated arrow 90 degrees
            arrow.style.transform = "rotate(-90deg)";

            arrow_div.appendChild(arrow);

            team_container.appendChild(team_picture);
            team_container.appendChild(team_name);
            team_container.appendChild(arrow_div);

            infoContainer.appendChild(team_container);
        }
    } else {
        infoContainer.classList.add("flex-center");
        infoContainer.innerHTML = "<p style='text-align: center;'>Er zijn nog geen teams</p>";
    }
};