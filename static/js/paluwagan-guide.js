/* Paluwagan onboarding guide — a Notion page shown in a full-screen overlay.
 *
 * Kept out of paluwagan.js on purpose: that bundle is the signing/navigation
 * logic and is exercised by a node harness; the guide is pure presentation.
 *
 * Three deliberate rules:
 *   1. The iframe src is copied from `data-src` on FIRST OPEN only. The Notion
 *      embed pulls ~11 external scripts, so a member who never opens the guide
 *      never pays for it.
 *   2. Auto-open happens ONCE per browser, and the seen-flag is written at
 *      reveal (not at close) — closing it without reading must not bring it
 *      back. Same one-shot rule as the L&E guide and the join-community banner.
 *   3. It does NOT auto-open for a guest or on an invite deep link. A guest
 *      gets the shared connect modal, and someone who tapped an invite link
 *      wants the join card immediately — stacking two overlays hides both.
 */
(function () {
    'use strict';

    var SEEN_KEY = 'gm_paluwagan_guide_seen_v1';

    function _boot() {
        var b = window.GM_PALUWAGAN_BOOT || {};
        return b;
    }

    function _overlay() { return document.getElementById('paluGuideOverlay'); }

    // A blocked localStorage (hardened private mode) must not throw here. When
    // we cannot read the flag we treat the guide as already seen: re-showing it
    // on every load would break the one-time promise.
    function _seen() {
        try { return localStorage.getItem(SEEN_KEY) === '1'; } catch (_) { return true; }
    }

    function _markSeen() {
        try { localStorage.setItem(SEEN_KEY, '1'); } catch (_) {}
    }

    function openPaluwaganGuide() {
        var ov = _overlay();
        if (!ov) return;
        var frame = document.getElementById('paluGuideFrame');
        if (frame && !frame.getAttribute('src')) {
            var src = frame.getAttribute('data-src');
            if (src) frame.setAttribute('src', src);
        }
        ov.classList.add('open');
        document.body.style.overflow = 'hidden';
        _markSeen();
    }

    function closePaluwaganGuide() {
        var ov = _overlay();
        if (!ov) return;
        ov.classList.remove('open');
        document.body.style.overflow = '';
    }

    window.openPaluwaganGuide = openPaluwaganGuide;
    window.closePaluwaganGuide = closePaluwaganGuide;

    function _maybeAutoOpen() {
        var boot = _boot();
        if (!boot.guideUrl) return;
        if (!_overlay()) return;
        // A guest gets the connect modal; an invite deep link wants the join
        // card. Never compete with either.
        if (boot.isGuest || boot.invite) return;
        if (_seen()) return;
        openPaluwaganGuide();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _maybeAutoOpen);
    } else {
        _maybeAutoOpen();
    }

    // Escape closes the guide, matching the chatroom panel's dismissal rule.
    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        var ov = _overlay();
        if (ov && ov.classList.contains('open')) closePaluwaganGuide();
    });
})();
