export const handleButtonClick = function(
    socket, isAutoScrolling, button, buttons, extraData, statsName
) {
    if (isAutoScrolling) {
        return;
    }

    buttons.forEach(el => el.classList.remove("active"));
    button.classList.add("active");

    isAutoScrolling = true;
    button.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
    setTimeout(() => isAutoScrolling = false, 500);

    sendDataToServer(socket, button, extraData, statsName);

    return isAutoScrolling;
};

function sendDataToServer(socket, button, extraData, statsName) {
    const data = button.getAttribute('data');
    const payload = { 'command': data };

    // Merge ExtraData into the payload if ExtraData is provided
    if (extraData) {
        Object.assign(payload, extraData);
    }

    if (data === statsName) {
        Object.assign(payload, { 'data_type': 'general' });
    }

    socket.send(JSON.stringify(payload));
};