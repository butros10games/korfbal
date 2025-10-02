export function pageReturn() {
    const returnButton = document.getElementById('return-button');

    if (
        globalThis.location.pathname === '/catalog/' &&
        localStorage.getItem('pageStack') === '["/catalog/"]'
    ) {
        returnButton.style.display = 'none';
    }

    returnButton.addEventListener('click', () => {
        const storedStack = localStorage.getItem('pageStack');
        const pageStack = storedStack ? JSON.parse(storedStack) : [];

        pageStack.pop();

        const previousPage = pageStack.pop();

        localStorage.setItem('pageStack', JSON.stringify(pageStack));

        if (previousPage) {
            globalThis.location.href = previousPage;
        } else {
            globalThis.location.href = '/catalog/';
        }
    });
}
