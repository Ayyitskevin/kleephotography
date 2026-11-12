/* Full-page prospect navigation: announce the destination/error, without changing writes. */
(function () {
  "use strict";
  var header = document.querySelector('.admin-topbar');
  function keepHeaderClear() {
    document.documentElement.style.setProperty('--prospect-focus-top',
      ((header ? header.getBoundingClientRect().height : 0) + 16) + 'px');
  }
  keepHeaderClear();
  if (header && window.ResizeObserver) new ResizeObserver(keepHeaderClear).observe(header);
  var target = document.querySelector('[data-prospect-focus]');
  // Do not steal focus if someone has already started interacting while loading.
  if (target && (!document.activeElement || document.activeElement === document.body)) target.focus();
})();
