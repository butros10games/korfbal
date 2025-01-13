export const truncateMiddle = function (text, maxLength) {
    if (text.length <= maxLength) {
        return text;
    }

    // Calculate the number of characters to show before and after the ellipsis
    const charsToShow = maxLength - 3;
    const frontChars = Math.ceil(charsToShow / 2);
    const backChars = Math.floor(charsToShow / 2);

    return text.substr(0, frontChars) + '...' + text.substr(text.length - backChars);
};
