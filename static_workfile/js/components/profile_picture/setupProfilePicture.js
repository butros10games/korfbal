import { uploadImage } from '../../utils/network/index.js';

export const setupProfilePicture = function (csrfToken) {
    const imageModal = document.getElementById('imageModal');
    const closeModalButton = document.getElementById('closeModal');

    // Show modal when an image is selected
    const fileInput = document.getElementById('profilePicInput');
    const imagePreview = document.getElementById('imagePreview');
    if (fileInput) {
        fileInput.addEventListener('change', async () => {
            const file = fileInput.files[0];
            if (file) {
                let blob = file;

                // Check if the file is a HEIC file
                if (file.name.toLowerCase().endsWith('.heic')) {
                    try {
                        // Convert HEIC to JPEG
                        blob = await heic2any({
                            blob: file,
                            toType: 'image/jpeg',
                            quality: 0.7, // Adjust quality as needed
                        });
                    } catch (error) {
                        console.error('Error converting HEIC to JPEG:', error);
                        return;
                    }
                }

                const reader = new FileReader();
                reader.onload = function (e) {
                    imagePreview.src = e.target.result;
                    imageModal.style.display = 'flex'; // Show the modal
                };
                reader.readAsDataURL(blob);
            }
        });
    }

    // Close modal when the close button is clicked
    if (closeModalButton) {
        closeModalButton.addEventListener('click', () => {
            imageModal.style.display = 'none'; // Hide the modal
        });
    }

    // Add event listener for save button
    const saveButton = document.getElementById('saveProfilePic');
    if (saveButton) {
        saveButton.addEventListener('click', () => {
            const file = fileInput.files[0];
            if (file) {
                const blob = file;

                // Convert HEIC to JPEG before upload if necessary
                if (file.name.toLowerCase().endsWith('.heic')) {
                    heic2any({
                        blob: file,
                        toType: 'image/jpeg',
                        quality: 0.7,
                    })
                        .then((convertedBlob) => {
                            uploadImage(convertedBlob, imageModal);
                        })
                        .catch((error) => {
                            console.error('Error converting HEIC to JPEG:', error);
                        });
                } else {
                    uploadImage(blob, imageModal);
                }
            }
        });
    }
};
