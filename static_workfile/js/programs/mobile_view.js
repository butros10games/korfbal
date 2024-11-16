const setFullHeight = () => {
    document.documentElement.style.setProperty('--vh', `${window.innerHeight * 0.01}px`);
};

setFullHeight(); // Initial set
window.addEventListener('resize', setFullHeight); // Reset on resize
window.addEventListener('orientationchange', setFullHeight); // Reset on orientation change