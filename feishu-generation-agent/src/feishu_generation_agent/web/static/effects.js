/* Mouse trail effect for sub-apps (optional, follows the Portal toggle).
 * Disabled by default so scrollable workbenches have no full-viewport overlay.
 * The fixed canvas is created only while the effect is enabled.
 */
(function () {
  'use strict';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (window.matchMedia && window.matchMedia('(hover: none)').matches) return;

  var FX_KEY = 'portal_fx_trail';
  var MAX_PARTICLES = 40;
  var IDLE_STOP_MS = 300;
  var TRAIL_RGB = '148,163,184';
  var BASE_ALPHA = 0.35;

  var canvas = null;
  var ctx = null;
  var particles = [];
  var running = false;
  var rafId = 0;
  var lastMoveAt = 0;
  var enabled = localStorage.getItem(FX_KEY) === 'on';
  var dpr = Math.max(1, Math.min(1.5, window.devicePixelRatio || 1));

  function resize() {
    if (!canvas || !ctx) return;
    canvas.width = Math.floor(window.innerWidth * dpr);
    canvas.height = Math.floor(window.innerHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function ensureCanvas() {
    if (canvas) return;
    canvas = document.createElement('canvas');
    canvas.id = '_fxTrail';
    canvas.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:60;';
    ctx = canvas.getContext('2d');
    resize();
    (document.body || document.documentElement).appendChild(canvas);
  }

  function removeCanvas() {
    stop();
    if (canvas) {
      canvas.remove();
      canvas = null;
      ctx = null;
    }
  }

  function spawn(x, y) {
    if (particles.length >= MAX_PARTICLES) particles.shift();
    particles.push({
      x: x, y: y,
      life: 1,
      decay: 0.05 + Math.random() * 0.03,
      size: 2.5 + Math.random() * 1.5
    });
  }

  function tick() {
    if (!ctx) return;
    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
    for (var i = particles.length - 1; i >= 0; i--) {
      var p = particles[i];
      p.life -= p.decay;
      if (p.life <= 0) { particles.splice(i, 1); continue; }
      var r = p.size * p.life;
      ctx.fillStyle = 'rgba(' + TRAIL_RGB + ',' + (p.life * BASE_ALPHA) + ')';
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();
    }

    if (particles.length === 0 && (performance.now() - lastMoveAt) > IDLE_STOP_MS) {
      running = false;
      ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
      return;
    }
    rafId = requestAnimationFrame(tick);
  }

  function start() {
    if (running || !ctx) return;
    running = true;
    rafId = requestAnimationFrame(tick);
  }

  function stop() {
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    running = false;
    particles.length = 0;
    if (ctx) ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  }

  function onMove(e) {
    lastMoveAt = performance.now();
    spawn(e.clientX, e.clientY);
    start();
  }

  function enableTrail() {
    ensureCanvas();
    window.addEventListener('mousemove', onMove, { passive: true });
  }

  function disableTrail() {
    window.removeEventListener('mousemove', onMove);
    removeCanvas();
  }

  function applyEnabled(next) {
    if (next === enabled) return;
    enabled = next;
    if (enabled) enableTrail(); else disableTrail();
  }

  function init() {
    window.addEventListener('storage', function (e) {
      if (e.key !== FX_KEY) return;
      applyEnabled((e.newValue || 'off') === 'on');
    });
    window.addEventListener('resize', function () { if (canvas) resize(); }, { passive: true });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) stop();
    });
    if (enabled) enableTrail();
  }

  if (document.body) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
