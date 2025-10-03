export const handleTouchStart = function (e, carouselElement, currentPosition) {
    const touchStartX = e.touches[0].clientX;
    carouselElement.style.transition = 'none';

    return {
        touchStartX,
        startPosition: currentPosition,
        isDragging: true,
    };
};

export const handleTouchMove = function (
    e,
    isDragging,
    touchStartX,
    carouselElement,
    startPosition,
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

export const handleTouchEnd = function (
    isDragging,
    touchStartX,
    touchEndX,
    currentPosition,
    carouselElement,
    buttonWidth,
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
        -(carouselElement.scrollWidth - carouselElement.clientWidth),
    );
    currentPosition = Math.min(currentPosition, 0);

    carouselElement.style.transition = 'transform 0.3s ease';
    carouselElement.style.transform = `translateX(${currentPosition}px)`;

    return {
        isDragging: false,
        currentPosition,
    };
};

// Button actions

export const handleButtonClick = function (
    socket,
    isAutoScrolling,
    button,
    buttons,
    extraData,
    statsName,
) {
    if (isAutoScrolling) {
        return;
    }

    for (const el of buttons) {
        el.classList.remove('active');
    }
    button.classList.add('active');

    isAutoScrolling = true;
    button.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    setTimeout(() => (isAutoScrolling = false), 500);

    sendDataToServer(socket, button, extraData, statsName);

    return isAutoScrolling;
};

function sendDataToServer(socket, button, extraData, statsName) {
    const data = button.getAttribute('data');
    const payload = { command: data };

    // Merge ExtraData into the payload if ExtraData is provided
    if (extraData) {
        Object.assign(payload, extraData);
    }

    if (data === statsName) {
        Object.assign(payload, { data_type: 'general' });
    }

    socket.send(JSON.stringify(payload));
}
