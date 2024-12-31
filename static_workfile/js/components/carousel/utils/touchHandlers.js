export const handleTouchStart = function(e, carouselElement, currentPosition) {
    const touchStartX = e.touches[0].clientX;
    carouselElement.style.transition = 'none';

    return {
        touchStartX,
        startPosition: currentPosition,
        isDragging: true,
    };
};

export const handleTouchMove = function(
    e, isDragging, touchStartX, carouselElement, startPosition
) {
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
};

export const handleTouchEnd = function(
    isDragging, touchStartX, touchEndX, currentPosition, carouselElement, buttonWidth
) {
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
};
