export const removePlayerClickHandlers = function(playerButtons) {
    Array.from(playerButtons).forEach(element => {
        element.style.background = "";
        if (element.playerClickHandler) {
            element.removeEventListener("click", element.playerClickHandler);
            delete element.playerClickHandler;
        }
    });
};