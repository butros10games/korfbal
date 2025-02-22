export function pageReturn() {
    const returnButton = document.getElementById('return-button');

    if (window.location.pathname === '/catalog/' && localStorage.getItem('pageStack') === '["/catalog/"]') {
        returnButton.style.display = 'none';
    }

    returnButton.addEventListener('click', () => {
        const storedStack = localStorage.getItem('pageStack');
        let pageStack = storedStack ? JSON.parse(storedStack) : [];

        pageStack.pop();

        const previousPage = pageStack.pop();

        localStorage.setItem('pageStack', JSON.stringify(pageStack));

        if (previousPage) {
            window.location.href = previousPage;
        } else {
            window.location.href = '/catalog/';
        }
    });
}
