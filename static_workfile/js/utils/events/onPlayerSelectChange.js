export const onPlayerSelectChange = function (changedSelect) {
    const allSelectors = document.querySelectorAll('.player-selector');
    for (const select of allSelectors) {
        // Skip the select that was changed
        if (select === changedSelect) {
            continue;
        }

        // If another select has the same value, reset it
        if (select.value === changedSelect.value) {
            select.value = Number.NaN; // Set to 'Niet ingevuld' value
        }
    }

    // Show the save button
    document.getElementById('saveButton').style.display = 'block';
};
