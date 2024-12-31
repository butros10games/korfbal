export const updateSettings = function(data, infoContainer, socket) {
    const profilePicture = document.getElementById('profilePic-container');
    profilePicture.classList.add('active-img');

    // Main container for settings
    const settingsContainer = document.createElement('div');
    settingsContainer.classList.add('flex-column');

    const settingsRow = document.createElement('div');
    settingsRow.classList.add('flex-row');

    const settingsText = document.createElement('div');
    settingsText.classList.add('from-container');

    // Create input field for each setting
    const fields = [
        { name: 'username', label: 'Username', type: 'text' },
        { name: 'email', label: 'Email', type: 'email' },
        { name: 'first_name', label: 'First Name', type: 'text' },
        { name: 'last_name', label: 'Last Name', type: 'text' },
        { name: 'email_2fa', label: '2FA Enabled', type: 'checkbox' }
    ];

    fields.forEach(field => {
        const inputShell = document.createElement('div');

        if (field.type === 'checkbox') {
            inputShell.classList.add('text-checkbox-shell');
        } else {
            inputShell.classList.add('text-input-shell');
        }

        const label = document.createElement('label');
        label.for = field.name;
        label.innerHTML = field.label;

        inputShell.appendChild(label);

        let input;
        if (field.type === 'checkbox') {
            input = document.createElement('input');
            input.type = 'checkbox';
            input.checked = data[field.name] ? data[field.name] : false;
            input.classList.add('text-checkbox');
        } else {
            input = document.createElement('input');
            input.type = field.type;
            input.value = data[field.name] && data[field.name].trim() !== '' ? data[field.name] : '';
            input.placeholder = field.label + '...';
            input.classList.add('text-input');
        }
        input.id = field.name;
        input.name = field.name;

        inputShell.appendChild(input);
        settingsText.appendChild(inputShell);
    });

    const saveButton = document.createElement('button');
    saveButton.type = 'submit';

    const saveButtonText = document.createElement('p');
    saveButtonText.innerHTML = 'Save';
    saveButtonText.style.margin = '0';
    saveButtonText.id = 'save-button-text';

    saveButton.appendChild(saveButtonText);

    saveButton.classList.add('save-button');

    saveButton.addEventListener('click', (event) => {
        event.preventDefault(); // Prevent form submission

        saveButton.classList.add('loading');

        // Gather input values
        const formData = {
            'username': document.getElementById('username').value,
            'email': document.getElementById('email').value,
            'first_name': document.getElementById('first_name').value,
            'last_name': document.getElementById('last_name').value,
            'email_2fa': document.getElementById('email_2fa').checked
        };

        // Send data to server
        socket.send(JSON.stringify({
            'command': 'settings_update',
            'data': formData
        }));
    });

    settingsText.appendChild(saveButton);

    // django logout button
    const logoutButton = document.createElement('a');
    logoutButton.href = '/logout';
    logoutButton.innerHTML = 'Logout';
    logoutButton.classList.add('logout-button');

    settingsText.appendChild(logoutButton);

    settingsRow.appendChild(settingsText);
    settingsContainer.appendChild(settingsRow);

    // Append the settingsContainer to the main container (assuming it's named infoContainer)
    infoContainer.innerHTML = ''; // Clear existing content
    infoContainer.appendChild(settingsContainer);
};
