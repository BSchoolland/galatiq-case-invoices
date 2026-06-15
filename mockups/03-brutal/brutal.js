// Opt into the staggered entrance only when motion is allowed.
// Without this, .reveal elements stay fully visible (see brutal.css).
(function () {
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduce) document.documentElement.classList.add('js-anim');
})();
