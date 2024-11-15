"use strict";

export const cleanDomCarousel = function(container) {
    container.innerHTML = "";
    container.classList.remove("flex-center");
    container.classList.remove("flex-start-wrap");
};