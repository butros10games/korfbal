'use strict';

export const handleTouchMove = function(e, isDragging, touchStartX, carouselElement, startPosition) {
    if (!isDragging) {
        return;
    }

    const touchEndX = e.touches[0].clientX;
    const diff = touchEndX - touchStartX;
    const currentPosition = startPosition + diff;
    carouselElement.style.transform = `translateX(${currentPosition}px)`;

    return {
        touchEndX,
        currentPosition,
    };
}