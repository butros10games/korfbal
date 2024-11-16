self.addEventListener('fetch', function(event) {
    // This is a minimal no-op service worker.
});

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/js/pwa/service-worker.js')
        .then(registration => {
            console.log('ServiceWorker registration successful with scope: ', registration.scope);

            // Listen for updates to the service worker
            registration.onupdatefound = () => {
                const installingWorker = registration.installing;
                installingWorker.onstatechange = () => {
                    if (installingWorker.state === 'installed') {
                        if (navigator.serviceWorker.controller) {
                            // New update available, refresh the page to load the new content
                            console.log('New content is available; refreshing.');
                            window.location.reload();
                        } else {
                            // Content is cached for offline use
                            console.log('Content is cached for offline use.');
                        }
                    }
                };
            };
        })
        .catch(err => {
            console.log('ServiceWorker registration failed: ', err);
        });
}
