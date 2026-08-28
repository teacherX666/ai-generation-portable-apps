/* ============================================================
 * dino.js — 页眉像素猫跳一跳 + 本地皮肤衣柜
 * ------------------------------------------------------------
 * 16×16 固定像素骨架，皮肤只改变色板/花纹/特效；跳跃物理与碰撞盒
 * 继续沿用原版 12×13 尺寸。当前测试版皮肤及选择保存在前端，选择结果
 * 写入 localStorage，不接后端、不影响任务和统计。
 * ============================================================ */
(function () {
  'use strict';

  var W = 320, H = 30;
  var GROUND_Y = 25;
  var DINO_X = 16;
  var HIT_W = 12, HIT_H = 13; // 保持原碰撞体积
  var SPRITE_W = 16, SPRITE_H = 15;
  var GRAVITY = 0.16;
  var JUMP_V = -2.3;
  var SPEED0 = 1.5, SPEED_INC = 0.0007, SPEED_MAX = 3.0;
  var HI_KEY = 'portal_dino_best';
  var SKIN_KEY = 'portal_cat_skin';

  var SKINS = [
    {
      id: 'classic-black', name: '经典黑猫', rarity: '默认', rarityClass: 'default',
      description: '最初陪大家跳栅栏的黑猫。',
      palette: { O: '#111827', B: '#1f2937', S: '#475569', P: '#111827', E: '#f8fafc', A: '#334155' },
      effect: 'none'
    },
    {
      id: 'banana-milk', name: '香蕉奶昔猫', rarity: '稀有', rarityClass: 'rare',
      description: '经过茶水间时，总能闻到香蕉牛奶。',
      palette: { O: '#50391e', B: '#f3cf4c', S: '#fff0ad', P: '#a66a2c', E: '#287c68', A: '#ffd960' },
      effect: 'none'
    },
    {
      id: 'midnight-nebula', name: '午夜星云猫', rarity: '史诗', rarityClass: 'epic',
      description: '夜色毛发里藏着几颗会闪烁的星星。',
      palette: { O: '#29243f', B: '#42366f', S: '#7156b8', P: '#d7b8ff', E: '#8ffff2', A: '#a778ff' },
      effect: 'star'
    },
    {
      id: 'porcelain-ink', name: '青花墨尾猫', rarity: '传说', rarityClass: 'legendary',
      description: '瓷白身体上的蓝纹，会在奔跑时化成墨点。',
      palette: { O: '#18355d', B: '#f4f1e8', S: '#dce8ed', P: '#245d9e', E: '#72e3ff', A: '#2c79c7' },
      effect: 'ink'
    }
  ];

  // 字符含义：O 轮廓、B 主色、S 次色、P 花纹、E 眼睛、A 强调色。
  // 两个动作帧只改变最下面的腿，身体轮廓和皮肤数据完全相同。
  var SPRITE_BODY = [
    '..........O..O..',
    '.........OOOOOO.',
    '.........OBBBOO.',
    '........OBBBBEO.',
    '..OOOOOOOBBBBBO.',
    '.OOBBBBBBBBBBBO.',
    'OOBPBBPBBSSSBBO.',
    'AOBBBBBBBSSSBBO.',
    'AOBBPBBBOOOOOOO.',
    '..OOBBBBBO......',
    '...OBBBBBO......'
  ];
  var LEGS_A = [
    '...OSB.OSB......',
    '...OSB.OSB......',
    '..OOB..OOB......',
    '..OO...OOO......'
  ];
  var LEGS_B = [
    '....OSBOSB......',
    '....OSBOSB......',
    '....OOB.OOB.....',
    '...OOO..OO......'
  ];

  function injectStyles() {
    if (document.getElementById('_catWardrobeStyles')) return;
    var style = document.createElement('style');
    style.id = '_catWardrobeStyles';
    style.textContent =
      '#_dinoWrap{position:relative;display:flex;align-items:center;flex:0 0 auto;margin:-6px 8px;user-select:none}' +
      '#_catWardrobeBtn{width:27px;height:27px;display:grid;place-items:center;margin-right:5px;padding:0;border:1px solid var(--border,#d8dee8);border-radius:8px;background:var(--surface,#fff);color:var(--muted,#64748b);cursor:pointer;box-shadow:0 1px 2px rgba(15,23,42,.05)}' +
      '#_catWardrobeBtn:hover,#_catWardrobeBtn[aria-expanded="true"]{color:var(--accent,#2563eb);border-color:#b8cef3;background:var(--accent-soft,#eff6ff)}' +
      '#_catWardrobeBtn svg{width:16px;height:16px;display:block}' +
      '#_dinoCanvas{display:block;cursor:pointer}' +
      '#_catWardrobe{position:fixed;z-index:10000;top:0;left:0;width:420px;padding:14px;border:1px solid var(--border,#d8dee8);border-radius:16px;background:var(--surface,#fff);color:var(--text,#172033);box-shadow:0 18px 50px rgba(15,23,42,.18);cursor:default}' +
      '#_catWardrobe[hidden]{display:none}' +
      '._catWardrobeHead{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px}' +
      '._catWardrobeTitle{font-size:15px;font-weight:800;line-height:1.25}' +
      '._catWardrobeSub{margin-top:3px;font-size:11px;color:var(--muted,#64748b)}' +
      '._catWardrobeClose{width:27px;height:27px;border:0;border-radius:8px;background:transparent;color:var(--muted,#64748b);font-size:18px;line-height:1;cursor:pointer}' +
      '._catWardrobeClose:hover{background:#f1f5f9;color:var(--text,#172033)}' +
      '._catSkinGrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}' +
      '._catSkinCard{display:grid;grid-template-columns:52px minmax(0,1fr);gap:10px;align-items:center;min-width:0;min-height:70px;padding:9px;border:1px solid var(--border,#d8dee8);border-radius:12px;background:var(--surface,#fff);color:inherit;text-align:left;cursor:pointer}' +
      '._catSkinCard:hover{border-color:#a9bfe8;background:#f8fafc}' +
      '._catSkinCard.is-active{border-color:var(--accent,#2563eb);box-shadow:0 0 0 2px rgba(37,99,235,.12);background:var(--accent-soft,#eff6ff)}' +
      '._catSkinPreview{width:52px;height:52px;border-radius:10px;display:grid;place-items:center;background-color:#f5f7fb;background-image:linear-gradient(45deg,#e2e8f0 25%,transparent 25%),linear-gradient(-45deg,#e2e8f0 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e2e8f0 75%),linear-gradient(-45deg,transparent 75%,#e2e8f0 75%);background-size:12px 12px;background-position:0 0,0 6px,6px -6px,-6px 0}' +
      '._catSkinPreview canvas{width:46px;height:46px;image-rendering:pixelated}' +
      '._catSkinName{font-size:12px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '._catSkinRarity{display:inline-block;margin-top:4px;padding:2px 6px;border-radius:999px;font-size:9px;font-weight:800}' +
      '._catSkinRarity.default{background:#eef2f6;color:#536174}' +
      '._catSkinRarity.rare{background:#e8f7ef;color:#16724a}' +
      '._catSkinRarity.epic{background:#eee9ff;color:#6941c6}' +
      '._catSkinRarity.legendary{background:#fff0d5;color:#a65300}' +
      '._catWardrobeFoot{margin-top:10px;font-size:10px;color:var(--muted,#64748b);text-align:center}' +
      '@media(max-width:700px){#_dinoWrap{margin-left:0}#_catWardrobe{left:12px!important;right:12px;width:auto!important}._catSkinGrid{grid-template-columns:1fr 1fr}}' +
      '@media(max-width:430px){._catSkinGrid{grid-template-columns:1fr}}';
    document.head.appendChild(style);
  }

  function init() {
    var bar = document.querySelector('.portal-bar');
    if (!bar || document.getElementById('_dinoCanvas')) return;
    injectStyles();

    var wrap = document.createElement('span');
    wrap.id = '_dinoWrap';

    var wardrobeButton = document.createElement('button');
    wardrobeButton.id = '_catWardrobeBtn';
    wardrobeButton.type = 'button';
    wardrobeButton.title = '打开猫咪衣柜';
    wardrobeButton.setAttribute('aria-label', '打开猫咪衣柜');
    wardrobeButton.setAttribute('aria-expanded', 'false');
    wardrobeButton.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="4" y="3" width="16" height="18" rx="2"/><path d="M12 3v18M8.5 12h.01M15.5 12h.01M7 7h2M15 7h2" stroke-linecap="round"/></svg>';
    wrap.appendChild(wardrobeButton);

    var canvas = document.createElement('canvas');
    canvas.id = '_dinoCanvas';
    canvas.title = '像素猫跳一跳 · 空格/点击起跳';
    var dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    wrap.appendChild(canvas);

    var wardrobe = document.createElement('div');
    wardrobe.id = '_catWardrobe';
    wardrobe.hidden = true;
    wardrobe.setAttribute('role', 'dialog');
    wardrobe.setAttribute('aria-label', '猫咪皮肤衣柜');
    wardrobe.innerHTML = '<div class="_catWardrobeHead"><div><div class="_catWardrobeTitle">猫咪衣柜</div><div class="_catWardrobeSub">选择一只猫陪你跳栅栏</div></div><button class="_catWardrobeClose" type="button" aria-label="关闭衣柜">×</button></div><div class="_catSkinGrid"></div><div class="_catWardrobeFoot">测试版 · 皮肤选择保存在当前浏览器</div>';
    wrap.appendChild(wardrobe);

    // 放到左侧信息和右侧状态之间。
    bar.insertBefore(wrap, bar.lastElementChild);
    // fixed 定位弹层若仍挂在带 backdrop-filter 的顶栏内，会形成新的包含块，
    // 进而被下方导航栏覆盖。把弹层移到 body，保证它真正位于全局最上层。
    document.body.appendChild(wardrobe);

    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = false;

    function cssVar(name, fallback) {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      return (v || '').trim() || fallback;
    }
    var COL_GROUND, COL_MUTED;
    function refreshColors() {
      COL_GROUND = cssVar('--border', '#94a3b8');
      COL_MUTED = cssVar('--muted', '#94a3b8');
    }
    refreshColors();

    var savedSkinId = localStorage.getItem(SKIN_KEY);
    var activeSkin = SKINS.filter(function (s) { return s.id === savedSkinId; })[0] || SKINS[0];

    function drawSprite(targetCtx, skin, x, y, legFrame, scale) {
      var rows = SPRITE_BODY.concat(legFrame ? LEGS_B : LEGS_A);
      scale = scale || 1;
      for (var py = 0; py < rows.length; py++) {
        for (var px = 0; px < SPRITE_W; px++) {
          var code = rows[py].charAt(px);
          if (code === '.') continue;
          targetCtx.fillStyle = skin.palette[code] || skin.palette.B;
          targetCtx.fillRect(Math.round(x + px * scale), Math.round(y + py * scale), scale, scale);
        }
      }
    }

    function renderWardrobe() {
      var grid = wardrobe.querySelector('._catSkinGrid');
      grid.textContent = '';
      SKINS.forEach(function (skin) {
        var card = document.createElement('button');
        card.type = 'button';
        card.className = '_catSkinCard' + (skin.id === activeSkin.id ? ' is-active' : '');
        card.setAttribute('data-skin-id', skin.id);
        card.setAttribute('aria-pressed', skin.id === activeSkin.id ? 'true' : 'false');
        card.title = skin.description;
        card.innerHTML = '<span class="_catSkinPreview"><canvas width="48" height="48"></canvas></span><span><span class="_catSkinName"></span><span class="_catSkinRarity ' + skin.rarityClass + '"></span></span>';
        card.querySelector('._catSkinName').textContent = skin.name;
        card.querySelector('._catSkinRarity').textContent = skin.rarity;
        var preview = card.querySelector('canvas').getContext('2d');
        preview.imageSmoothingEnabled = false;
        drawSprite(preview, skin, 0, 0, 0, 3);
        card.addEventListener('click', function (e) {
          e.stopPropagation();
          activeSkin = skin;
          localStorage.setItem(SKIN_KEY, skin.id);
          renderWardrobe();
          draw();
        });
        grid.appendChild(card);
      });
    }

    function positionWardrobe() {
      var buttonRect = wardrobeButton.getBoundingClientRect();
      var barRect = bar.getBoundingClientRect();
      var width = Math.min(420, window.innerWidth - 24);
      var left = Math.max(12, Math.min(buttonRect.left, window.innerWidth - width - 12));
      var top = Math.max(barRect.bottom + 8, 8);
      wardrobe.style.width = width + 'px';
      wardrobe.style.left = left + 'px';
      wardrobe.style.top = top + 'px';
    }

    function setWardrobeOpen(open) {
      wardrobe.hidden = !open;
      wardrobeButton.setAttribute('aria-expanded', open ? 'true' : 'false');
      if (open) {
        renderWardrobe();
        positionWardrobe();
      }
    }

    wardrobeButton.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      setWardrobeOpen(wardrobe.hidden);
    });
    wardrobe.querySelector('._catWardrobeClose').addEventListener('click', function (e) {
      e.stopPropagation();
      setWardrobeOpen(false);
      wardrobeButton.focus();
    });
    wardrobe.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', function (e) {
      if (!wardrobe.hidden && !wrap.contains(e.target) && !wardrobe.contains(e.target)) setWardrobeOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !wardrobe.hidden) {
        setWardrobeOpen(false);
        wardrobeButton.focus();
      }
    });

    var STATE = { IDLE: 0, PLAY: 1, DEAD: 2 };
    var state = STATE.IDLE;
    var dinoY = GROUND_Y - HIT_H;
    var vy = 0;
    var speed = SPEED0;
    var obstacles = [];
    var particles = [];
    var spawnIn = 40;
    var frame = 0;
    var score = 0;
    var best = parseInt(localStorage.getItem(HI_KEY) || '0', 10) || 0;
    var groundShift = 0;
    var rafId = 0;

    function reset() {
      dinoY = GROUND_Y - HIT_H;
      vy = 0;
      speed = SPEED0;
      obstacles = [];
      particles = [];
      spawnIn = 40;
      frame = 0;
      score = 0;
      groundShift = 0;
    }
    function grounded() { return dinoY >= GROUND_Y - HIT_H - 0.01; }
    function jump() { if (grounded()) vy = JUMP_V; }
    function start() { reset(); state = STATE.PLAY; loop(); }
    function spawn() {
      var h = 6 + Math.floor(Math.random() * 3);
      var w = 5 + Math.floor(Math.random() * 4);
      obstacles.push({ x: W + 2, w: w, h: h });
      spawnIn = 95 + Math.floor(Math.random() * 75);
    }
    function spawnParticle() {
      if (activeSkin.effect === 'none' || frame % 12 !== 0) return;
      particles.push({
        x: DINO_X + 1, y: dinoY + 8,
        vx: -0.25 - Math.random() * 0.35,
        vy: -0.2 + Math.random() * 0.4,
        life: 18,
        color: activeSkin.effect === 'star' ? activeSkin.palette.P : activeSkin.palette.A
      });
    }
    function step() {
      frame++;
      score = Math.floor(frame / 5);
      speed = Math.min(SPEED_MAX, speed + SPEED_INC);
      groundShift = (groundShift + speed) % 12;
      vy += GRAVITY;
      dinoY += vy;
      if (dinoY > GROUND_Y - HIT_H) { dinoY = GROUND_Y - HIT_H; vy = 0; }
      if (--spawnIn <= 0) spawn();
      for (var i = obstacles.length - 1; i >= 0; i--) {
        obstacles[i].x -= speed;
        if (obstacles[i].x + obstacles[i].w < -2) obstacles.splice(i, 1);
      }
      spawnParticle();
      for (var p = particles.length - 1; p >= 0; p--) {
        particles[p].x += particles[p].vx;
        particles[p].y += particles[p].vy;
        if (--particles[p].life <= 0) particles.splice(p, 1);
      }
      var dx = DINO_X + 2, dw = HIT_W - 4;
      var dy = dinoY + 2, dh = HIT_H - 4;
      for (var j = 0; j < obstacles.length; j++) {
        var o = obstacles[j];
        var ox = o.x + 1, ow = o.w - 2;
        var oy = GROUND_Y - o.h + 1, oh = o.h - 2;
        if (dx < ox + ow && dx + dw > ox && dy < oy + oh && dy + dh > oy) {
          gameOver();
          return;
        }
      }
    }
    function gameOver() {
      state = STATE.DEAD;
      if (score > best) { best = score; localStorage.setItem(HI_KEY, String(best)); }
      draw();
    }

    function drawCat() {
      var visualTop = Math.round(dinoY) - (SPRITE_H - HIT_H);
      var runningFrame = state === STATE.PLAY && grounded() && Math.floor(frame / 7) % 2;
      drawSprite(ctx, activeSkin, DINO_X, visualTop, runningFrame, 1);
    }
    function drawParticles() {
      for (var i = 0; i < particles.length; i++) {
        var p = particles[i];
        ctx.globalAlpha = Math.max(0, p.life / 18);
        ctx.fillStyle = p.color;
        if (activeSkin.effect === 'star' && p.life > 10) {
          ctx.fillRect(Math.round(p.x) - 1, Math.round(p.y), 3, 1);
          ctx.fillRect(Math.round(p.x), Math.round(p.y) - 1, 1, 3);
        } else {
          ctx.fillRect(Math.round(p.x), Math.round(p.y), 2, 2);
        }
      }
      ctx.globalAlpha = 1;
    }
    function drawGround() {
      ctx.strokeStyle = COL_GROUND;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, GROUND_Y + 0.5);
      ctx.lineTo(W, GROUND_Y + 0.5);
      ctx.stroke();
      ctx.fillStyle = COL_GROUND;
      for (var gx = -groundShift; gx < W; gx += 12) ctx.fillRect(Math.round(gx), GROUND_Y + 3, 3, 1);
    }
    function drawObstacles() {
      ctx.fillStyle = cssVar('--text', '#17191f');
      for (var i = 0; i < obstacles.length; i++) {
        var o = obstacles[i];
        ctx.fillRect(Math.round(o.x), GROUND_Y - o.h, o.w, o.h);
        ctx.fillRect(Math.round(o.x) - 2, GROUND_Y - o.h + 3, 2, 2);
        ctx.fillRect(Math.round(o.x) + o.w, GROUND_Y - o.h + 2, 2, 2);
      }
    }
    function drawScore() {
      ctx.fillStyle = COL_MUTED;
      ctx.font = '10px ui-monospace,Menlo,Consolas,monospace';
      ctx.textAlign = 'right';
      ctx.textBaseline = 'top';
      var hi = best ? 'HI ' + String(best).padStart(4, '0') + '  ' : '';
      ctx.fillText(hi + String(score).padStart(4, '0'), W, 0);
      ctx.textAlign = 'left';
    }
    function drawHint(text) {
      ctx.fillStyle = COL_MUTED;
      ctx.font = '10px ui-sans-serif,system-ui,sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, W / 2, H / 2 - 1);
      ctx.textAlign = 'left';
    }
    function draw() {
      ctx.clearRect(0, 0, W, H);
      drawGround();
      drawObstacles();
      drawParticles();
      drawCat();
      drawScore();
      if (state === STATE.IDLE) drawHint('点击 / 空格 开始');
      else if (state === STATE.DEAD) drawHint('GAME OVER · 点击重开');
    }
    function loop() {
      if (state !== STATE.PLAY) return;
      step();
      if (state === STATE.PLAY) {
        draw();
        rafId = requestAnimationFrame(loop);
      }
    }
    function onAction() { if (state === STATE.PLAY) jump(); else start(); }

    canvas.addEventListener('click', function (e) { e.preventDefault(); onAction(); });
    function isTyping() {
      var el = document.activeElement;
      if (!el) return false;
      var tag = el.tagName;
      return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || tag === 'BUTTON' || el.isContentEditable;
    }
    window.addEventListener('keydown', function (e) {
      if (e.code !== 'Space' && e.key !== ' ' && e.code !== 'ArrowUp' && e.key !== 'ArrowUp') return;
      if (isTyping() || !wardrobe.hidden) return;
      e.preventDefault();
      onAction();
    });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden && rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      else if (!document.hidden && state === STATE.PLAY && !rafId) loop();
    });
    window.addEventListener('resize', function () { refreshColors(); if (!wardrobe.hidden) positionWardrobe(); if (state !== STATE.PLAY) draw(); });

    renderWardrobe();
    draw();
  }

  if (document.querySelector('.portal-bar')) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
