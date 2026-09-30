// Native details owns the open state; these handlers only dismiss the toolbox.
// Do not close on outside clicks: filter/calendar popovers can render in portals.
(function () {
    function closeTools(returnFocus) {
        var toolbox = document.getElementById('toolbox');
        if (!toolbox || !toolbox.open) return;
        toolbox.open = false;
        if (returnFocus) toolbox.querySelector('.toolbox-trigger').focus();
    }

    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape' || event.defaultPrevented) return;
        if (document.querySelector('#toolbox .dash-dropdown[aria-expanded="true"]')) return;
        closeTools(true);
    });

    document.addEventListener('click', function (event) {
        if (event.target.closest('#open-collection-filters')) {
            var filters = document.getElementById('collection-filters');
            if (filters) filters.open = true;
        }
        var action = event.target.closest('a[href], #search-free, #answer-paid, .toolbox-close');
        if (!action || action.disabled) return;
        closeTools(action.tagName !== 'A');
    });

    window.addEventListener('popstate', function () { closeTools(false); });
    // Plots need their visible width after a tab or disclosure opens.
    document.addEventListener('toggle', function (event) {
        if (event.target.open) window.dispatchEvent(new Event('resize'));
    }, true);
    var observer = new MutationObserver(function (changes) {
        if (changes.some(function (change) { return change.attributeName === 'hidden'; })) {
            window.requestAnimationFrame(function () { window.dispatchEvent(new Event('resize')); });
        }
    });
    observer.observe(document.documentElement, {subtree:true, attributes:true, attributeFilter:['hidden']});
}());
