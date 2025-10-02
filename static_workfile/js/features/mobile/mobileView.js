const setFullHeight = () => {
    document.documentElement.style.setProperty(
        '--vh',
        `${globalThis.innerHeight * 0.01}px`,
    );
};

setFullHeight(); // Initial set
globalThis.addEventListener('resize', setFullHeight); // Reset on resize
globalThis.addEventListener('orientationchange', setFullHeight); // Reset on orientation change
