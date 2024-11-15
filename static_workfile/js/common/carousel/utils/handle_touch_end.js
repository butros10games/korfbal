"use strict";

export const handleTouchEnd = function(isDragging, touchStartX, touchEndX, currentPosition, carouselElement, buttonWidth) {
    if (!isDragging) {
        return;
    }

    const diff = touchEndX - touchStartX;
    if (diff > buttonWidth / 3) {
        currentPosition += buttonWidth;
    } else if (diff < -buttonWidth / 3) {
        currentPosition -= buttonWidth;
    }

    currentPosition = Math.max(
        currentPosition,
        -(carouselElement.scrollWidth - carouselElement.clientWidth)
    );
    currentPosition = Math.min(currentPosition, 0);

    carouselElement.style.transition = 'transform 0.3s ease';
    carouselElement.style.transform = `translateX(${currentPosition}px)`;

    return {
        isDragging: false,
        currentPosition,
    };
}