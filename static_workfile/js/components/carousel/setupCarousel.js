import {
    handleTouchStart,
    handleTouchMove,
    handleTouchEnd,
    handleButtonClick,
} from './utils/index.js';

export const setupCarousel = function (
    carouselElement,
    buttons,
    socket,
    extraData = null,
    statsName = null,
) {
    let isDragging = false;
    let touchStartX = 0;
    let touchEndX = 0;
    let currentPosition = 0;
    let startPosition = 0;
    let isAutoScrolling = false;
    const buttonWidth = document.querySelector('.button').offsetWidth;

    carouselElement.addEventListener('touchstart', (e) => {
        const result = handleTouchStart(e, carouselElement, currentPosition);

        touchStartX = result.touchStartX;
        startPosition = result.startPosition;
        isDragging = result.isDragging;
    });
    carouselElement.addEventListener('touchmove', (e) => {
        const result = handleTouchMove(
            e,
            isDragging,
            touchStartX,
            carouselElement,
            startPosition,
        );

        touchEndX = result.touchEndX;
        currentPosition = result.currentPosition;
    });
    carouselElement.addEventListener('touchend', (e) => {
        const result = handleTouchEnd(
            isDragging,
            touchStartX,
            touchEndX,
            currentPosition,
            carouselElement,
            buttonWidth,
        );

        isDragging = result.isDragging;
        currentPosition = result.currentPosition;
    });

    // setup nav buttons
    buttons.forEach((button, _) => {
        button.addEventListener('click', () => {
            isAutoScrolling = handleButtonClick(
                socket,
                isAutoScrolling,
                button,
                buttons,
                extraData,
                statsName,
            );
            // Reset isAutoScrolling after handling the button click
            isAutoScrolling = false;
        });
    });
};