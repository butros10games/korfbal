"use strict";

export const handleTouchStart = function(e, carouselElement, currentPosition) {
    const touchStartX = e.touches[0].clientX;
    carouselElement.style.transition = 'none';

    return {
        touchStartX,
        startPosition: currentPosition,
        isDragging: true,
    };
}