/* Theme toggle — light/dark via [data-theme] on <html>.
 * The pre-render FOUC script (inlined in each template's <head>)
 * already applied the right theme by the time this loads. This file
 * only wires up the toggle button.
 */

(function () {
    function getTheme() {
        return document.documentElement.getAttribute('data-theme') || 'dark';
    }

    function setTheme(t) {
        document.documentElement.setAttribute('data-theme', t);
        try { localStorage.setItem('theme', t); } catch (_) {}
        updateButton();
    }

    function updateButton() {
        const btn = document.getElementById('themeToggle');
        if (!btn) return;
        const t = getTheme();
        // Show the icon of the theme you'd switch TO.
        btn.innerHTML = (t === 'dark')
            ? '<i class="fas fa-sun"></i>'
            : '<i class="fas fa-moon"></i>';
        btn.title = (t === 'dark') ? 'Switch to light' : 'Switch to dark';
    }

    function init() {
        const btn = document.getElementById('themeToggle');
        if (btn) btn.addEventListener('click', () => {
            setTheme(getTheme() === 'dark' ? 'light' : 'dark');
        });
        // Track OS preference changes only if the user hasn't pinned a choice.
        if (window.matchMedia) {
            const mq = window.matchMedia('(prefers-color-scheme: dark)');
            mq.addEventListener('change', (e) => {
                if (!localStorage.getItem('theme')) {
                    setTheme(e.matches ? 'dark' : 'light');
                }
            });
        }
        updateButton();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
