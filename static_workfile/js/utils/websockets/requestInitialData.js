export const requestInitialData = function(buttonSelector, socket, moreData = null) {
    const button = document.querySelector(buttonSelector);
    if (button) {
        const data = button.getAttribute('data');
        const payload = { 'command': data };

        // Merge moreData into the payload if moreData is provided
        if (moreData) {
            Object.assign(payload, moreData);
        }

        socket.send(JSON.stringify(payload));
    }
};
