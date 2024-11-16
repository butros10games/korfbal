import { deleteConfirmPopup } from "./utils";

export const deleteButtonSetup = function(socket) {
    const deleteButton = document.getElementById('deleteButton');

    deleteButton.addEventListener('click', () => {
        deleteConfirmPopup(socket);
    });
};