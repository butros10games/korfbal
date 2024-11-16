"use strict";

export const setupSwipeDelete = function() {
    const matchEvent = document.getElementById('match-event-swipe');
    const swipeContent = document.getElementById('match-event');
    swipeContent.style.transform = `translateX(0px)`;
    let startX, currentX, isSwiping = false;

    const onTouchStart = (e) => {
        const transform = window.getComputedStyle(swipeContent).getPropertyValue('transform');
        const transformX = transform.split(',')[4].trim();

        startX = e.touches[0].clientX - parseInt(transformX);
        isSwiping = true;
        swipeContent.classList.remove('transition-back');
    };

    const onTouchMove = (e) => {
        if (!isSwiping) {return;};

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
        swipeContent.style.transform = isSwipeLeft ? 'translateX(-100px)' : 'translateX(0px)';
        swipeContent.classList.add('transition-back');
        matchEvent.classList.toggle('swiped-left', isSwipeLeft);
    };

    swipeContent.addEventListener('touchstart', onTouchStart, { passive: true });
    swipeContent.addEventListener('touchmove', onTouchMove, { passive: true });
    swipeContent.addEventListener('touchend', onTouchEnd, false);
}