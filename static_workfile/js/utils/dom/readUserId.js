export const readUserId = function () {
    const user_id = document.getElementById('user_id').innerText.trim();

    if (user_id === 'None') {
        return null;
    }

    return Number.parseInt(user_id);
};
