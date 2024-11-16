export const onPlayerSelectChange = function(changedSelect) {
    const allSelectors = document.querySelectorAll('.player-selector');
    allSelectors.forEach(select => {
        // Skip the select that was changed
        if (select === changedSelect) {return;};

        // If another select has the same value, reset it
        if (select.value === changedSelect.value) {
            select.value = NaN; // Set to 'Niet ingevuld' value
        }
    });

    // Show the save button
    document.getElementById("saveButton").style.display = "block";
};