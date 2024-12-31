export class CountdownTimer {
    constructor(startTimeISO,
        lengthInMilliseconds,
        pauseTimeISO = null,
        offsetInMilliseconds = 0,
        showEndHalfButton = false,
    ) {
        this.lengthInMilliseconds = lengthInMilliseconds;

        this.startTime = new Date(startTimeISO);
        this.totalLength = lengthInMilliseconds + offsetInMilliseconds; // Include the offset
        this.endTime = new Date(this.startTime.getTime() + this.totalLength);

        this.offset = offsetInMilliseconds;
        this.pauseTimeISO = pauseTimeISO;
        this.interval = null;
        this.showEndHalfButton = showEndHalfButton;

        // Call updateDisplay immediately upon construction to set the initial value
        this.updateDisplay();
    }

    updateDisplay() {
        this.now = this.pauseTimeISO ? new Date(this.pauseTimeISO) : new Date();
        const timeLeft = this.totalLength - (this.now - this.startTime);

        const sign = timeLeft < 0 ? '-' : '';
        const absTime = Math.abs(timeLeft);

        const minutes = Math.floor(absTime / 60000);
        const seconds = Math.floor((absTime % 60000) / 1000);

        // Update the counter display on the website
        document.getElementById('counter').innerText = `${sign}${minutes}:${seconds.toString().padStart(2, '0')}`;

        // Conditionally display the "end-half-button"
        if (this.showEndHalfButton) {
            const endHalfButton = document.getElementById('end-half-button');
            if (minutes < 1 || sign === '-') {
                endHalfButton.style.display = 'block';
            } else if (minutes > 1 && endHalfButton.style.display === 'block') {
                endHalfButton.style.display = 'none';
            }
        }
    }

    start(pause_time = null) {
        if (pause_time) {
            this.totalLength = this.lengthInMilliseconds + (pause_time * 1000);
        }

        this.pauseTimeISO = null;
        this.interval = setInterval(() => this.updateDisplay(), 1000);
    }

    stop() {
        clearInterval(this.interval);
        this.interval = null;
    }

    destroy() {
        clearInterval(this.interval);
        this.interval = null;
        this.startTime = null;
        this.endTime = null;
        this.now = null;
        this.totalLength = null;
        this.offset = null;
        this.pauseTimeISO = null;
    }
}
