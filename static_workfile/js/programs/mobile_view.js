const setFullHeight = () => {
    document.documentElement.style.setProperty('--vh', `${window.innerHeight * 0.01}px`);
}

// Initial set
setFullHeight();

// Reset on resize
window.addEventListener('resize', setFullHeight);