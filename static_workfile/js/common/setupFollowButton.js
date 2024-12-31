export const setupFollowButton = function(id, socket) {
    document.querySelector('.icon-container').addEventListener('click', function() {
        const isFollowed = this.getAttribute('data-followed') === 'true';

        // Toggle the data-followed attribute
        this.setAttribute('data-followed', !isFollowed);

        socket.send(JSON.stringify({
            'command': 'follow',
            'user_id': id,
            'followed': !isFollowed
        }));
    });
};
