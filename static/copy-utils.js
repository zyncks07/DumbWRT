/* copy-utils.js — one-click copy for [data-copy] elements, all pages.
 *
 * Usage: add data-copy="<value>" data-copy-label="IP|MAC" to any element.
 * The script handles the rest via event delegation — no per-element JS needed.
 *
 * Strategy:
 *   1. Try navigator.clipboard.writeText (modern, async, requires secure context).
 *   2. Fall back to document.execCommand('copy') via a temporary textarea
 *      (works on HTTP, Android WebView, IE11-era browsers, older mobile Safari).
 *   3. Show a fixed-position toast near the click point on success.
 *
 * Event listener runs in capture phase so it fires before any bubble-phase
 * onclick handlers (e.g. the dashboard row-expand toggleRouter call), and
 * calls stopPropagation so those handlers do not also fire.
 */
(function () {
    'use strict';

    /* ---- Inject shared CSS ---- */
    var s = document.createElement('style');
    s.textContent =
        '[data-copy]{cursor:pointer;user-select:none}' +
        '[data-copy]:hover{opacity:.72}' +
        '#_ct{position:fixed;padding:4px 11px;border-radius:5px;' +
            'font-size:11px;font-weight:700;letter-spacing:.02em;' +
            'pointer-events:none;z-index:99999;white-space:nowrap;' +
            'background:var(--ok,#00ff88);color:var(--bg-0,#0a0e27);' +
            'box-shadow:0 2px 10px rgba(0,0,0,.4);' +
            'opacity:0;transition:opacity .14s ease}' +
        '#_ct.on{opacity:1}';
    document.head.appendChild(s);

    /* ---- Toast ---- */
    var toast = null;
    var hideTimer = null;

    function getToast() {
        if (!toast) {
            toast = document.createElement('div');
            toast.id = '_ct';
            toast.setAttribute('role', 'status');
            toast.setAttribute('aria-live', 'polite');
            document.body.appendChild(toast);
        }
        return toast;
    }

    function showToast(label, cx, cy) {
        var t = getToast();
        t.textContent = label + ' copied!';
        t.className = '';               // hide, measure, then position
        t.style.left = '0px';
        t.style.top  = '0px';

        requestAnimationFrame(function () {
            var w  = t.offsetWidth;
            var h  = t.offsetHeight;
            var vw = window.innerWidth  || document.documentElement.clientWidth;
            var vh = window.innerHeight || document.documentElement.clientHeight;

            var x = cx + 14;
            var y = cy - h - 8;
            if (x + w > vw - 8) x = cx - w - 14;  // flip left if too close to right edge
            if (y < 8)          y = cy + 14;       // flip below if too close to top

            t.style.left = x + 'px';
            t.style.top  = y + 'px';
            t.className  = 'on';

            clearTimeout(hideTimer);
            hideTimer = setTimeout(function () { t.className = ''; }, 1500);
        });
    }

    /* ---- Clipboard write with execCommand fallback ---- */
    function execFallback(text) {
        var ta = document.createElement('textarea');
        ta.value = text;
        /* Keep it out of the viewport but still in the DOM so select() works */
        ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;' +
                           'width:1px;height:1px;opacity:0;font-size:16px';
        ta.setAttribute('readonly', '');
        ta.setAttribute('aria-hidden', 'true');
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        ta.setSelectionRange(0, ta.value.length); /* required on iOS */
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (_) { /* ignore */ }
        document.body.removeChild(ta);
        return ok;
    }

    function doCopy(text, label, cx, cy) {
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            navigator.clipboard.writeText(text).then(
                function ()  { showToast(label, cx, cy); },
                function ()  { if (execFallback(text)) showToast(label, cx, cy); }
            );
        } else {
            if (execFallback(text)) showToast(label, cx, cy);
        }
    }

    /* ---- Single delegated listener, capture phase ---- */
    document.addEventListener('click', function (e) {
        var el = e.target.closest('[data-copy]');
        if (!el) return;
        e.stopPropagation();   /* prevent parent onclick (row-expand, etc.) */
        var text  = el.getAttribute('data-copy')       || '';
        var label = el.getAttribute('data-copy-label') || 'Value';
        if (text) doCopy(text, label, e.clientX, e.clientY);
    }, true /* capture */);

    /* Backwards-compat: any existing inline callers can use window.copyText */
    window.copyText = doCopy;

}());
