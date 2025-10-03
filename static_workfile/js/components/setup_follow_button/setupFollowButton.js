export const setupFollowButton = function (id, socket) {
    document.querySelector('.icon-container').addEventListener('click', function () {
        const isFollowed = this.dataset.followed === 'true';

        // Toggle the data-followed attribute
        this.dataset.followed = (!isFollowed).toString();

        socket.send(
            JSON.stringify({
                command: 'follow',
                user_id: id,
                followed: !isFollowed,
            }),
        );
    });
};
