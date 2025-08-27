export const initializeSocket = function (url, onMessageCallback, onOpenCallback) {
    let socket;
    const socketWrapper = {
        send(data) {
            // Always forward to the latest socket instance
            if (socket && socket.readyState === WebSocket.OPEN) {
                socket.send(data);
            } else {
                console.warn('Socket is not open. Could not send:', data);
            }
        },
    };

    function connect() {
        if (socket) {
            // Avoid duplicates or old references
            socket.onclose = null;
            socket.close();
        }

        socket = new WebSocket(url);

        // The onopen that is called every time, including reconnect
        socket.onopen = function () {
            console.log('Connection established!');
            if (typeof onOpenCallback === 'function') {
                onOpenCallback(socket);
            }
        };

        // use the renamed parameter here
        socket.onmessage = onMessageCallback;

        socket.onclose = function (event) {
            if (!event.wasClean) {
                console.error('Connection died, attempting to reconnect...');
            } else {
                console.log(
                    `Connection closed cleanly, code=${event.code}, reason=${event.reason}`,
                );
            }
            setTimeout(connect, 3000);
        };

        socket.onerror = function (error) {
            console.error('WebSocket error:', error);
            socket.close();
        };
    }

    connect();
    return socketWrapper;
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
