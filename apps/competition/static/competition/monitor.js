// Refresh is opt-in and pauses while the operator edits the date or season.
const refresh = document.getElementById('monitor-refresh');
const preference = 'competition-monitor-refresh';
let timer;
function configureRefresh() {
    clearInterval(timer);
    if (refresh.checked) {
        timer = setInterval(() => {
            if (!document.hidden && !document.activeElement?.matches('#date, #season')) {
                window.location.reload();
            }
        }, 60000);
    }
}
if (refresh) {
    try {
        refresh.checked = sessionStorage.getItem(preference) === 'true';
    } catch {
        // Storage can be disabled; refreshing still works for the current page.
    }
    configureRefresh();
    refresh.addEventListener('change', () => {
        try {
            sessionStorage.setItem(preference, String(refresh.checked));
        } catch {
            // Keep the control usable without browser storage.
        }
        configureRefresh();
    });
}
