// Utility functions for scoring button components (no circular deps)
export const getButtonBackground = (team, isActive) => {
    if (team === 'home') {
        return isActive ? '#43ff64' : '#43ff6480';
    }
    return isActive ? 'rgba(235, 0, 0, 0.7)' : 'rgba(235, 0, 0, 0.5)';
};
