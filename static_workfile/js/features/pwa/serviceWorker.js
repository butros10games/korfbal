self.addEventListener('fetch', (event) => {
    // This is a minimal no-op service worker.
});

if ('serviceWorker' in navigator) {
    try {
        const registration = await navigator.serviceWorker.register(
            `https://static.${globalThis.location.hostname}/js/pwa/service-worker.js`,
        );
        console.log(
            'ServiceWorker registration successful with scope: ',
            registration.scope,
        );

        // Listen for updates to the service worker
        registration.onupdatefound = () => {
            const installingWorker = registration.installing;
            installingWorker.onstatechange = () => {
                if (installingWorker.state === 'installed') {
                    if (navigator.serviceWorker.controller) {
                        // New update available, refresh the page to load the new content
                        console.log('New content is available; refreshing.');
                        globalThis.location.reload();
                    } else {
                        // Content is cached for offline use
                        console.log('Content is cached for offline use.');
                    }
                }
            };
        };
    } catch (err) {
        console.log('ServiceWorker registration failed: ', err);
    }
}
