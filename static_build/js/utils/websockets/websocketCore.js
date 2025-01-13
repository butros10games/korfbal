export const initializeSocket = function (url, onMessageReceived) {
    let socket;

    function connect() {
        if (socket) {
            socket.onclose = null;
            socket.close();
        }

        socket = new WebSocket(url);

        socket.onopen = function () {
            console.log('Connection established!');
        };

        socket.onmessage = onMessageReceived;

        socket.onclose = function (event) {
            if (!event.wasClean) {
                console.error('Connection died, attempting to reconnect...');
                setTimeout(connect, 3000);
            } else {
                console.log(
                    `Connection closed cleanly, code=${event.code}, reason=${event.reason}`,
                );
            }
        };
    }

    connect();
    return socket;
};

export const requestInitialData = function (buttonSelector, socket, moreData = null) {
    const button = document.querySelector(buttonSelector);
    if (button) {
        const data = button.getAttribute('data');
        const payload = { command: data };

        // Merge moreData into the payload if moreData is provided
        if (moreData) {
            Object.assign(payload, moreData);
        }

        socket.send(JSON.stringify(payload));
    }
};

export function onMessageReceived(commandHandlers) {
    return function (event) {
        const data = JSON.parse(event.data);
        const { command } = data;
    
        if (commandHandlers[command]) {
            commandHandlers[command](data);
        } else {
            console.warn('No handler for command:', command);
        }
    };
}
