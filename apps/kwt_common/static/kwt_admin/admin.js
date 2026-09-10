const directorySearch = document.getElementById('admin-directory-search');
if (directorySearch) {
    const groups = [...document.querySelectorAll('#content-main .module')];
    directorySearch.addEventListener('input', () => {
        const term = directorySearch.value.trim().toLocaleLowerCase();
        let visible = 0;
        for (const group of groups) {
            const caption =
                group.querySelector('caption')?.textContent.toLocaleLowerCase() || '';
            let matches = 0;
            for (const row of group.querySelectorAll('tbody tr')) {
                const label =
                    row.querySelector('th')?.textContent.toLocaleLowerCase() || '';
                row.hidden = !`${caption} ${label}`.includes(term);
                if (!row.hidden) matches++;
            }
            group.hidden = matches === 0;
            visible += matches;
        }
        document.getElementById('admin-directory-empty').hidden = visible > 0;
    });
}
