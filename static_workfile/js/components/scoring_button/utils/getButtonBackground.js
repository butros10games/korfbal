export const getButtonBackground = function(team, isActive) {
    if (team === 'home') {
        return isActive ? '#43ff64' : '#43ff6480';
    } else {
        return isActive ? 'rgba(235, 0, 0, 0.7)' : 'rgba(235, 0, 0, 0.5)';
    }
};
