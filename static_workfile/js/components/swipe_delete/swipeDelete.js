export const deleteButtonSetup = function (socket) {
    const deleteButton = document.getElementById('deleteButton');

    deleteButton.addEventListener('click', () => {
        deleteConfirmPopup(socket);
    });
};

export const resetSwipe = function () {
    // Assuming swipeContent is the element you want to reset
    const swipeContent = document.getElementById('match-event');

    // Reset transform to initial state
    swipeContent.style.transform = 'translateX(0px)';

    // Reset any classes that might have been added or removed during swipe
    swipeContent.classList.remove('transition-back', 'swiped-left');
};

export const setupSwipeDelete = function () {
    const matchEvent = document.getElementById('match-event-swipe');
    const swipeContent = document.getElementById('match-event');
    swipeContent.style.transform = 'translateX(0px)';
    let startX,
        currentX,
        isSwiping = false;

    const onTouchStart = (e) => {
        const transform = globalThis
            .getComputedStyle(swipeContent)
            .getPropertyValue('transform');
        const transformX = transform.split(',')[4].trim();

        startX = e.touches[0].clientX - Number.parseInt(transformX);
        isSwiping = true;
        swipeContent.classList.remove('transition-back');
    };

    const onTouchMove = (e) => {
        if (!isSwiping) {
            return;
        }

        currentX = e.touches[0].clientX;
        const distance = startX - currentX;
        if (distance >= 0) {
            requestAnimationFrame(() => {
                swipeContent.style.transform = `translateX(${-Math.min(distance, 100)}px)`;
            });
        }
    };

    const onTouchEnd = () => {
        isSwiping = false;
        const swipeDistance = startX - currentX;
        const isSwipeLeft = swipeDistance > 50;
        swipeContent.style.transform = isSwipeLeft
            ? 'translateX(-100px)'
            : 'translateX(0px)';
        swipeContent.classList.add('transition-back');
        matchEvent.classList.toggle('swiped-left', isSwipeLeft);
    };

    swipeContent.addEventListener('touchstart', onTouchStart, { passive: true });
    swipeContent.addEventListener('touchmove', onTouchMove, { passive: true });
    swipeContent.addEventListener('touchend', onTouchEnd, false);
};

const deleteConfirmPopup = function (socket) {
    // Create the overlay container
    const overlay = document.createElement('div');
    overlay.id = 'overlay';
    overlay.classList.add('overlay');

    // Create the popup container
    const popup = document.createElement('div');
    popup.classList.add('popup');

    const popupTextRow = document.createElement('div');
    popupTextRow.classList.add('flex-row');
    popupTextRow.style.marginBottom = '24px';

    // Create the content for the popup
    const popupText = document.createElement('p');
    popupText.innerHTML = 'Event verwijderen?';
    popupText.style.margin = '0';
    popupText.style.fontSize = '20px';
    popupText.style.fontWeight = '600';

    // Create a close button for the popup
    const closeButton = document.createElement('button');
    closeButton.classList.add('close-button');
    closeButton.innerHTML = 'Close';
    closeButton.addEventListener('click', () => {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();
    });

    popupTextRow.appendChild(popupText);
    popupTextRow.appendChild(closeButton);

    popup.appendChild(popupTextRow);

    const popupButtonContainer = document.createElement('div');
    popupButtonContainer.classList.add('flex-row');
    popupButtonContainer.style.justifyContent = 'space-between';

    const popupButton = document.createElement('button');
    popupButton.classList.add('button');
    popupButton.innerHTML = 'Ja';
    popupButton.style.margin = '0';
    popupButton.style.width = 'calc(50% - 12px)';
    popupButton.style.height = '42px';
    popupButton.style.fontSize = '16px';
    popupButton.style.fontWeight = '600';
    popupButton.style.background = 'var(--button-color)';
    popupButton.style.color = 'var(--text-color)';
    popupButton.style.border = 'none';
    popupButton.style.borderRadius = '4px';
    popupButton.style.cursor = 'pointer';
    popupButton.style.userSelect = 'none';

    popupButton.addEventListener('click', () => {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();

        // remove the scroll lock
        document.body.style.overflow = '';

        // send the delete command to the server
        const data = {
            command: 'remove_last_event',
        };

        socket.send(JSON.stringify(data));
    });

    popupButtonContainer.appendChild(popupButton);

    const popupButton2 = document.createElement('button');
    popupButton2.classList.add('button');
    popupButton2.innerHTML = 'Nee';
    popupButton2.style.margin = '0';
    popupButton2.style.width = 'calc(50% - 12px)';
    popupButton2.style.height = '42px';
    popupButton2.style.fontSize = '16px';
    popupButton2.style.fontWeight = '600';
    popupButton2.style.background = 'red';
    popupButton2.style.color = 'var(--text-color)';
    popupButton2.style.border = 'none';
    popupButton2.style.borderRadius = '4px';
    popupButton2.style.cursor = 'pointer';
    popupButton2.style.userSelect = 'none';

    popupButton2.addEventListener('click', () => {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();

        // remove the scroll lock
        document.body.style.overflow = '';
    });

    popupButtonContainer.appendChild(popupButton2);

    popup.appendChild(popupButtonContainer);

    // Append the popup to the overlay
    overlay.appendChild(popup);

    // Append the overlay to the body to cover the entire screen
    document.body.appendChild(overlay);

    // Disable scrolling on the body while the overlay is open
    document.body.style.overflow = 'hidden';

    // add a event listener to the overlay so when clicked it closes the popup
    overlay.addEventListener('click', () => {
        // Remove the popup and overlay when the close button is clicked
        overlay.remove();

        // remove the scroll lock
        document.body.style.overflow = '';
    });

    // add a event listener to the popup so when clicked it doesn't close the overlay
    popup.addEventListener('click', (event) => {
        event.stopPropagation();
    });
};
