window.initializeSocket = function(url, onMessageReceived) {
    let socket;

    function connect() {
        if (socket) {
            socket.onclose = null;
            socket.close();
        }

        socket = new WebSocket(url);

        socket.onopen = function() {
            console.log("Connection established!");
        };

        socket.onmessage = onMessageReceived;

        socket.onclose = function(event) {
            if (!event.wasClean) {
                console.error('Connection died, attempting to reconnect...');
                setTimeout(connect, 3000);
            } else {
                console.log(`Connection closed cleanly, code=${event.code}, reason=${event.reason}`);
            }
        };
    }

    connect();
    return socket;
}

window.setupCarousel = function(carouselElement, buttons, ExtraData = null) {
    let isDragging = false;
    let touchStartX = 0;
    let touchEndX = 0;
    let currentPosition = 0;
    let startPosition = 0;
    let isAutoScrolling = false;
    const buttonWidth = document.querySelector('.button').offsetWidth;

    function handleTouchStart(e) {
        touchStartX = e.touches[0].clientX;
        startPosition = currentPosition;
        isDragging = true;
        carouselElement.style.transition = 'none';
    }

    function handleTouchMove(e) {
        if (!isDragging) return;
        touchEndX = e.touches[0].clientX;
        const diff = touchEndX - touchStartX;
        currentPosition = startPosition + diff;
        carouselElement.style.transform = `translateX(${currentPosition}px)`;
    }

    function handleTouchEnd() {
        if (!isDragging) return;

        const diff = touchEndX - touchStartX;
        if (diff > buttonWidth / 3) {
            currentPosition += buttonWidth;
        } else if (diff < -buttonWidth / 3) {
            currentPosition -= buttonWidth;
        }

        currentPosition = Math.max(currentPosition, -(carouselElement.scrollWidth - carouselElement.clientWidth));
        currentPosition = Math.min(currentPosition, 0);

        carouselElement.style.transition = 'transform 0.3s ease';
        carouselElement.style.transform = `translateX(${currentPosition}px)`;

        isDragging = false;
    }

    function setNavButtons() {
        buttons.forEach((button, index) => {
            button.addEventListener("click", function() {
                if (isAutoScrolling) return;

                buttons.forEach(el => el.classList.remove("active"));
                this.classList.add("active");

                isAutoScrolling = true;
                this.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
                setTimeout(() => isAutoScrolling = false, 500);

                // send data to server
                const data = this.getAttribute('data');
                const payload = { 'command': data };

                // Merge ExtraData into the payload if ExtraData is provided
                if (ExtraData) {
                    Object.assign(payload, ExtraData);
                }

                if (data === 'get_stats') {
                    Object.assign(payload, { 'data_type': 'general' });
                }

                socket.send(JSON.stringify(payload));
            });
        });
    }

    carouselElement.addEventListener('touchstart', handleTouchStart);
    carouselElement.addEventListener('touchmove', handleTouchMove);
    carouselElement.addEventListener('touchend', handleTouchEnd);

    setNavButtons();
}

window.requestInitalData = function(buttonSelector, socket, moreData = null) {
    const button = document.querySelector(buttonSelector);
    if (button) {
        const data = button.getAttribute('data');
        const payload = { 'command': data };

        // Merge moreData into the payload if moreData is provided
        if (moreData) {
            Object.assign(payload, moreData);
        }

        socket.send(JSON.stringify(payload));
    }
}

window.truncateMiddle = function(text, maxLength) {
    if (text.length <= maxLength) {
        return text;
    }
  
    // Calculate the number of characters to show before and after the ellipsis
    const charsToShow = maxLength - 3;
    const frontChars = Math.ceil(charsToShow / 2);
    const backChars = Math.floor(charsToShow / 2);
  
    return text.substr(0, frontChars) + '...' + text.substr(text.length - backChars);
}

window.cleanDom = function(container) {
    container.innerHTML = "";
    container.classList.remove("flex-center");
    container.classList.remove("flex-start-wrap");
}