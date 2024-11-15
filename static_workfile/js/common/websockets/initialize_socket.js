"use strict";

export const initializeSocket = function(url, onMessageReceived) {
    let socket;

    function connect() {
        if (socket) {
            socket.onclose = null;
            socket.close();
        }

        socket = new WebSocket(url);

        socket.onopen = function() {
            console.log("Connection established!");
        };

        socket.onmessage = onMessageReceived;

        socket.onclose = function(event) {
            if (!event.wasClean) {
                console.error('Connection died, attempting to reconnect...');
                setTimeout(connect, 3000);
            } else {
                console.log(`Connection closed cleanly, code=${event.code}, reason=${event.reason}`);
            }
        };
    }

    connect();
    return socket;
};