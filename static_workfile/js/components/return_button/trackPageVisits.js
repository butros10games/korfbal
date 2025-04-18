export function trackPageVisits() {
    const currentPage = window.location.pathname;
    
    // Retrieve the existing stack from local storage; if it doesn't exist, start with an empty array.
    const storedStack = localStorage.getItem("pageStack");
    let pageStack = storedStack ? JSON.parse(storedStack) : [];
    
    // Only add the current page if it’s not already the last page in the stack.
    if (pageStack.length === 0 || pageStack[pageStack.length - 1] !== currentPage) {
        pageStack.push(currentPage);
        localStorage.setItem("pageStack", JSON.stringify(pageStack));
    }
}
