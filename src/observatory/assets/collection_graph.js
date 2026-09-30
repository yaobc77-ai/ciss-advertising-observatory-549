// Expanded graph retains the same controls and record inspector.
(function () {
    'use strict';
    // Wait for an actual, nonzero canvas size after showing or expanding the
    // panel. The graph component handles canvas.resize; the server requests a
    // fit of its existing positions, avoiding hidden-viewport pan arithmetic.
    var graphObserver;
    var observedCanvas;
    var previousSize = '';
    var sizeRevision = 0;
    var resizeTimer;
    function observeGraph() {
        var canvas = document.getElementById('collection-graph-canvas');
        if (canvas === observedCanvas) return;
        if (graphObserver) graphObserver.disconnect();
        clearTimeout(resizeTimer);
        observedCanvas = canvas;
        previousSize = '';
        if (!canvas || !window.ResizeObserver) return;
        graphObserver = new ResizeObserver(function (entries) {
            var rect = entries[0].contentRect;
            var width = Math.round(rect.width), height = Math.round(rect.height);
            var client = window.dash_clientside;
            clearTimeout(resizeTimer);
            if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
                previousSize = '';
                return;
            }
            if (!client || !client.set_props) return;
            var size = width + ':' + height;
            if (size === previousSize) return;
            // Cytoscape's own resize observer clears its size cache after a
            // 100ms debounce. Fit after that update and after CSS has settled.
            resizeTimer = setTimeout(function () {
                previousSize = size;
                sizeRevision = sizeRevision % 99999 + 1;
                client.set_props('collection-graph-size', {data: {width: width, height: height, revision: sizeRevision}});
            }, 200);
        });
        graphObserver.observe(canvas);
    }
    var previousFocus;
    function observeSelection() {
        var heading = document.querySelector('#collection-graph-selection-heading [data-collection-focus]');
        var focus = heading && heading.getAttribute('data-collection-focus');
        if (focus === previousFocus) return;
        previousFocus = focus;
        var aside = document.querySelector('#collection-graph-stage .collection-graph-aside');
        if (aside) aside.scrollTop = 0;
    }
    function observeState() { observeGraph(); observeSelection(); }
    new MutationObserver(observeState).observe(document.documentElement, {
        childList: true, subtree: true, attributes: true, attributeFilter: ['data-collection-focus']
    });
    observeState();
    // Native buttons also support Enter/Space. The server validates bucket IDs.
    document.addEventListener('click', function (event) {
        var button = event.target.closest && event.target.closest('button[data-collection-bucket]');
        var client = window.dash_clientside;
        if (button && client && client.set_props) {
            client.set_props('collection-graph-bucket', {value: button.getAttribute('data-collection-bucket')});
        }
    });
    // Native <details> toggles its DOM state without updating Dash's open prop.
    // Bridge the real disclosure state through Dash's public set_props API.
    document.addEventListener('toggle', function (event) {
        if (event.target.id !== 'article-provenance-disclosure') return;
        var client = window.dash_clientside;
        if (client && client.set_props) {
            client.set_props('article-provenance-open', {data: event.target.open});
        }
    }, true);
    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape' || event.defaultPrevented) return;
        var panel = document.getElementById('collection-graph-panel');
        if (!panel || !panel.classList.contains('collection-graph-expanded')) return;
        if (panel.querySelector('[aria-expanded="true"]')) return;
        var exit = document.getElementById('collection-graph-expand');
        if (exit) { exit.click(); exit.focus(); event.preventDefault(); }
    });
}());
