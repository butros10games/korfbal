export const resetSwipe = function() {
    // Assuming swipeContent is the element you want to reset
    const swipeContent = document.getElementById('match-event');

    // Reset transform to initial state
    swipeContent.style.transform = 'translateX(0px)';

    // Reset any classes that might have been added or removed during swipe
    swipeContent.classList.remove('transition-back');
    swipeContent.classList.remove('swiped-left'); // If this class is added on swipe
};