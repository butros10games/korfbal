export const uploadImage = function(blob, csrfToken, imageModal) {
    const formData = new FormData();
    formData.append('profile_picture', blob, 'profile_picture.jpg'); // Set a default filename for the JPEG

    fetch('/profile/api/upload_profile_picture/', {
        method: 'POST',
        body: formData,
        headers: {
            'X-CSRFToken': csrfToken
        }
    })
        .then(response => response.json())
        .then(data => {
            imageModal.style.display = 'none'; // Hide the modal
            document.getElementById('profilePic').src = '/media' + data.url;
        })
        .catch(error => {
            console.error('Error:', error);
        });
};
