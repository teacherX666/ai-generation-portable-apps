/* ============================================================
 * dino.js — 页眉像素猫跳一跳 + 账号皮肤衣柜
 * ------------------------------------------------------------
 * 每只皮肤拥有独立的 16×16 两帧像素矩阵；跳跃物理与碰撞盒继续沿用
 * 原版 12×13 尺寸。衣柜、每日礼盒与装备状态以登录账号为单位保存。
 * ============================================================ */
(function () {
  'use strict';

  var W = 320, H = 30;
  var GROUND_Y = 25;
  var DINO_X = 16;
  var HIT_W = 12, HIT_H = 13; // 保持原碰撞体积
  var SPRITE_W = 16, SPRITE_H = 16;
  var GRAVITY = 0.16;
  var JUMP_V = -2.3;
  var SPEED0 = 1.5, SPEED_INC = 0.0007, SPEED_MAX = 3.0;
  var HI_KEY = 'portal_dino_best';
  var SKIN_KEY = 'portal_cat_skin';

  var CLASSIC_SKIN = {
    id: 'classic-black', name: '经典黑猫', rarity: 'common', rarity_label: '默认',
    description: '最初陪大家跳栅栏的经典黑猫。', effect: 'none',
    palette: { O:'#14161B', F:'#30343D', I:'#64E49A', P:'#141416', N:'#DB5369', S:'#5B6271' },
    frames: {
      a: ['................','.......O.......O','.......OSO...OSO','.......OFFFFFFFO','.......OFFFFFFFO','.......OFIPFIPFO','.OO....OFIPFIPFO','OFFO..FOFFFNFFFO','OOFFOFFFOFOSOFO.','.OFFOFFFFOOOOO..','..OFOFFFFFFSS...','...OOFFFFFFSO...','....OOFFFFOSS...','....OOFFOOOSS...','.....OFF..OSS...','.....OOO..OOO...'],
      b: ['................','.......O.......O','.......OSO...OSO','.......OFFFFFFFO','.......OFFFFFFFO','.......OFIPFIPFO','.OO....OFIPFIPFO','OFFO..FOFFFNFFFO','OOF.OFFFOFOSOFO.','.OF.OFFFFOOOOO..','..OFOFFFFFFSS...','...OOFFFFFFSO...','....OFF....SSO..','....OFFOOOOSSO..','....OFF....SSO..','....OOO....OOO..']
    }
  };
  var SKINS = [CLASSIC_SKIN];

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
      '._catWardrobeHead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}' +
      '._catWardrobeTitle{font-size:15px;font-weight:800;line-height:1.25}' +
      '._catWardrobeTitleWrap{min-width:0}' +
      '._catDailyTask{margin-top:3px;color:var(--muted,#64748b);font-size:10.5px;font-weight:650;line-height:1.35}' +
      '._catDailyTask.is-complete{color:#16845b}' +
      '._catWardrobeActions{display:flex;align-items:center;gap:3px}' +
      '._catWardrobeClose,._catGiftButton{width:27px;height:27px;border:0;border-radius:8px;background:transparent;color:var(--muted,#64748b);display:grid;place-items:center;padding:0;cursor:pointer}' +
      '._catWardrobeClose{font-size:18px;line-height:1}' +
      '._catWardrobeClose:hover,._catGiftButton:hover:not(:disabled){background:#f1f5f9;color:var(--text,#172033)}' +
      '._catGiftButton{font-size:16px;line-height:1}' +
      '._catGiftButton.is-spinning{animation:_catGiftSpin .7s linear infinite}' +
      '._catGiftButton:disabled{cursor:wait;opacity:.58}' +
      '@keyframes _catGiftSpin{to{transform:rotate(360deg)}}' +
      '._catGiftStatus{position:fixed;z-index:10002;left:50%;top:78px;max-width:min(360px,calc(100vw - 28px));padding:8px 12px;border-radius:10px;background:#172033;color:#fff;font-size:12px;font-weight:700;box-shadow:0 8px 24px rgba(15,23,42,.22);transform:translate(-50%,-8px);opacity:0;pointer-events:none;transition:.18s}' +
      '._catGiftStatus.has-message{transform:translate(-50%,0);opacity:1}' +
      '._catGiftStatus.is-error{background:#b42318}' +
      '#_catReveal{position:fixed;inset:0;z-index:10001;display:grid;place-items:center;padding:20px;background:rgba(15,23,42,.35);backdrop-filter:blur(3px)}' +
      '#_catReveal[hidden]{display:none}' +
      '._catRevealCard{position:relative;width:min(360px,calc(100vw - 32px));padding:22px;border:1px solid #e4d9ff;border-radius:22px;background:var(--surface,#fff);text-align:center;box-shadow:0 24px 70px rgba(15,23,42,.25);overflow:hidden}' +
      '._catRevealCard:before{content:"";position:absolute;inset:-45% -20%;background:radial-gradient(circle,#eee9ff 0,transparent 58%);pointer-events:none}' +
      '._catRevealContent{position:relative;z-index:1}' +
      '._catRevealRarity{display:inline-block;padding:4px 10px;border-radius:999px;font-size:11px;font-weight:850}' +
      '._catRevealRarity.common{background:#eef2f6;color:#536174}._catRevealRarity.rare{background:#e8f7ef;color:#16724a}._catRevealRarity.epic{background:#eee9ff;color:#6941c6}._catRevealRarity.legendary{background:#fff0d5;color:#a65300}' +
      '._catRevealTitle{margin:10px 0 0;font-size:20px;font-weight:850}' +
      '._catRevealCanvas{width:192px;height:192px;margin:14px auto 12px;image-rendering:pixelated;border-radius:16px;background-color:#f5f7fb;background-image:linear-gradient(45deg,#e2e8f0 25%,transparent 25%),linear-gradient(-45deg,#e2e8f0 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e2e8f0 75%),linear-gradient(-45deg,transparent 75%,#e2e8f0 75%);background-size:24px 24px;background-position:0 0,0 12px,12px -12px,-12px 0}' +
      '._catRevealHint{margin:0 0 16px;color:var(--muted,#64748b);font-size:11px}' +
      '._catRevealClose{position:absolute;right:12px;top:12px;z-index:2;width:28px;height:28px;border:0;border-radius:8px;background:transparent;color:var(--muted,#64748b);font-size:20px;cursor:pointer}' +
      '._catRevealClose:hover{background:#f1f5f9}' +
      '._catSkinGrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}' +
      '._catSkinCard{display:grid;grid-template-columns:52px minmax(0,1fr);gap:10px;align-items:center;min-width:0;min-height:70px;padding:9px;border:1px solid var(--border,#d8dee8);border-radius:12px;background:var(--surface,#fff);color:inherit;text-align:left;cursor:pointer}' +
      '._catSkinCard:hover{border-color:#a9bfe8;background:#f8fafc}' +
      '._catSkinCard.is-active{border-color:var(--accent,#2563eb);box-shadow:0 0 0 2px rgba(37,99,235,.12);background:var(--accent-soft,#eff6ff)}' +
      '._catSkinPreview{width:52px;height:52px;border-radius:10px;display:grid;place-items:center;background-color:#f5f7fb;background-image:linear-gradient(45deg,#e2e8f0 25%,transparent 25%),linear-gradient(-45deg,#e2e8f0 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e2e8f0 75%),linear-gradient(-45deg,transparent 75%,#e2e8f0 75%);background-size:12px 12px;background-position:0 0,0 6px,6px -6px,-6px 0}' +
      '._catSkinPreview canvas{width:46px;height:46px;image-rendering:pixelated}' +
      '._catSkinName{font-size:12px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '._catSkinRarity{display:inline-block;margin-top:4px;padding:2px 6px;border-radius:999px;font-size:9px;font-weight:800}' +
      '._catSkinRarity.default{background:#eef2f6;color:#536174}' +
      '._catSkinRarity.common{background:#eef2f6;color:#536174}' +
      '._catSkinRarity.rare{background:#e8f7ef;color:#16724a}' +
      '._catSkinRarity.epic{background:#eee9ff;color:#6941c6}' +
      '._catSkinRarity.legendary{background:#fff0d5;color:#a65300}' +
      '._catPager{display:flex;align-items:center;justify-content:center;gap:10px;margin-top:12px;min-height:28px}' +
      '._catPager[hidden]{display:none}' +
      '._catPageButton{width:28px;height:28px;border:1px solid var(--border,#d8dee8);border-radius:8px;background:var(--surface,#fff);color:var(--text,#172033);font-size:15px;cursor:pointer}' +
      '._catPageButton:hover:not(:disabled){border-color:#a9bfe8;background:#f8fafc}' +
      '._catPageButton:disabled{opacity:.35;cursor:default}' +
      '._catPageText{min-width:64px;text-align:center;color:var(--muted,#64748b);font-size:11px;font-weight:750}' +
      '#_catSkinDetail{position:fixed;z-index:10003;width:280px;padding:14px;border:1px solid #d8dee8;border-radius:16px;background:var(--surface,#fff);color:var(--text,#172033);box-shadow:0 20px 55px rgba(15,23,42,.24)}' +
      '#_catSkinDetail[hidden]{display:none}' +
      '._catDetailHead{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}' +
      '._catDetailIdentity{display:flex;align-items:center;gap:7px;min-width:0}' +
      '._catDetailName{font-size:14px;font-weight:850;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '._catDetailCanvasWrap{position:relative;width:224px;height:224px;margin:8px auto;border-radius:14px;overflow:hidden;background-color:#f5f7fb;background-image:linear-gradient(45deg,#e2e8f0 25%,transparent 25%),linear-gradient(-45deg,#e2e8f0 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e2e8f0 75%),linear-gradient(-45deg,transparent 75%,#e2e8f0 75%);background-size:28px 28px;background-position:0 0,0 14px,14px -14px,-14px 0}' +
      '._catDetailCanvas{display:block;width:224px;height:224px;image-rendering:pixelated}' +
      '._catDetailDescription{min-height:30px;margin:4px 2px 10px;color:var(--muted,#64748b);font-size:11px;line-height:1.45;text-align:center}' +
      '._catReleaseButton{width:100%;height:32px;border:1px solid #fecaca;border-radius:9px;background:#fff7f7;color:#b42318;font-size:12px;font-weight:800;cursor:pointer}' +
      '._catReleaseButton:hover{background:#feecec;border-color:#fda4af}' +
      '._catReleaseButton:disabled{opacity:.55;cursor:wait}' +
      '._catBuiltInHint{text-align:center;color:#98a2b3;font-size:10px}' +
      '._catLabButton{display:none;width:27px;height:27px;border:0;border-radius:8px;background:transparent;color:var(--muted,#64748b);font-size:15px;line-height:1;place-items:center;padding:0;cursor:pointer}' +
      '._catLabButton.is-visible{display:grid}' +
      '._catLabButton:hover{background:#f1f5f9;color:var(--text,#172033)}' +
      '#_catLab{position:fixed;inset:0;z-index:10004;display:grid;place-items:center;padding:20px;background:rgba(15,23,42,.42);backdrop-filter:blur(4px)}' +
      '#_catLab[hidden]{display:none}' +
      '._catLabPanel{width:min(760px,calc(100vw - 28px));max-height:calc(100vh - 40px);overflow:auto;padding:18px;border:1px solid var(--border,#d8dee8);border-radius:18px;background:var(--surface,#fff);color:var(--text,#172033);box-shadow:0 28px 80px rgba(15,23,42,.28)}' +
      '._catLabHead{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px}' +
      '._catLabTitle{font-size:17px;font-weight:850}' +
      '._catLabNote{margin-top:4px;color:var(--muted,#64748b);font-size:11px}' +
      '._catLabClose{width:30px;height:30px;border:0;border-radius:9px;background:transparent;color:var(--muted,#64748b);font-size:20px;cursor:pointer}' +
      '._catLabClose:hover{background:#f1f5f9}' +
      '._catLabControls{display:grid;grid-template-columns:minmax(180px,1fr) minmax(150px,.7fr) auto;gap:10px;align-items:end}' +
      '._catLabField{display:grid;gap:5px;color:var(--muted,#64748b);font-size:10px;font-weight:750}' +
      '._catLabField select,._catLabField input{height:36px;padding:0 10px;border:1px solid var(--border,#d8dee8);border-radius:9px;background:var(--surface,#fff);color:var(--text,#172033);font:inherit;font-size:12px}' +
      '._catLabGenerate{height:36px;padding:0 16px;border:0;border-radius:9px;background:var(--accent,#2563eb);color:#fff;font-size:12px;font-weight:800;cursor:pointer}' +
      '._catLabGenerate:disabled{opacity:.55;cursor:wait}' +
      '._catTrendStatus{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;padding:9px 10px;border-radius:10px;background:#f8fafc;color:#64748b;font-size:10px}' +
      '._catTrendDot{width:7px;height:7px;border-radius:50%;background:#f59e0b}' +
      '._catTrendDot.is-ok{background:#22c55e}' +
      '._catTrendRefresh{margin-left:auto;border:0;border-radius:7px;padding:5px 9px;background:#e8eef8;color:#31598f;font-size:10px;font-weight:800;cursor:pointer}' +
      '._catTrendRefresh:disabled{opacity:.55;cursor:wait}' +
      '._catLabResult{display:grid;grid-template-columns:270px minmax(0,1fr);gap:18px;margin-top:16px;padding-top:16px;border-top:1px solid var(--border,#d8dee8)}' +
      '._catLabCanvas{display:block;width:256px;height:256px;image-rendering:pixelated;border-radius:15px;background-color:#f5f7fb;background-image:linear-gradient(45deg,#e2e8f0 25%,transparent 25%),linear-gradient(-45deg,#e2e8f0 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e2e8f0 75%),linear-gradient(-45deg,transparent 75%,#e2e8f0 75%);background-size:32px 32px;background-position:0 0,0 16px,16px -16px,-16px 0}' +
      '._catLabMeta{display:grid;align-content:start;gap:10px}' +
      '._catLabBadge{justify-self:start;padding:4px 9px;border-radius:999px;background:#e8f7ef;color:#16724a;font-size:10px;font-weight:850}' +
      '._catLabBadge.is-error{background:#feecec;color:#b42318}' +
      '._catLabName{font-size:19px;font-weight:850}' +
      '._catLabStats{display:grid;grid-template-columns:1fr 1fr;gap:8px}' +
      '._catLabStat{padding:9px;border-radius:10px;background:#f8fafc;color:#475569;font-size:11px}' +
      '._catLabJson{max-height:170px;overflow:auto;margin:0;padding:10px;border-radius:10px;background:#111827;color:#d1fae5;font:10px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all}' +
      '@media(max-width:700px){._catLabControls{grid-template-columns:1fr}._catLabResult{grid-template-columns:1fr}._catLabCanvas{margin:auto}}' +
      '@media(max-width:700px){#_dinoWrap{margin-left:0}#_catWardrobe{left:12px!important;right:12px;width:auto!important}._catSkinGrid{grid-template-columns:1fr 1fr}#_catSkinDetail{width:260px}}' +
      '@media(max-width:430px){._catSkinGrid{grid-template-columns:1fr}#_catSkinDetail{left:12px!important;right:12px!important;bottom:12px!important;top:auto!important;width:auto!important}}';
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
    wardrobe.innerHTML = '<div class="_catWardrobeHead"><div class="_catWardrobeTitleWrap"><div class="_catWardrobeTitle">猫咪衣柜</div><div class="_catDailyTask">每日任务：完成一次飞书任务 Agent 生成任务</div></div><div class="_catWardrobeActions"><button class="_catLabButton" type="button" aria-label="管理员猫咪实验台" title="管理员猫咪实验台">🧪</button><button class="_catGiftButton" type="button" aria-label="打开礼盒" title="打开礼盒">🎁</button><button class="_catWardrobeClose" type="button" aria-label="关闭衣柜">×</button></div></div><div class="_catGiftStatus"></div><div class="_catSkinGrid"></div><div class="_catPager"><button class="_catPageButton _catPagePrev" type="button" aria-label="上一页">‹</button><span class="_catPageText"></span><button class="_catPageButton _catPageNext" type="button" aria-label="下一页">›</button></div>';
    wrap.appendChild(wardrobe);

    // 放到左侧信息和右侧状态之间。
    bar.insertBefore(wrap, bar.lastElementChild);
    // fixed 定位弹层若仍挂在带 backdrop-filter 的顶栏内，会形成新的包含块，
    // 进而被下方导航栏覆盖。把弹层移到 body，保证它真正位于全局最上层。
    document.body.appendChild(wardrobe);

    var skinDetail = document.createElement('div');
    skinDetail.id = '_catSkinDetail';
    skinDetail.hidden = true;
    skinDetail.setAttribute('role', 'dialog');
    skinDetail.setAttribute('aria-label', '猫咪详情');
    skinDetail.innerHTML = '<div class="_catDetailHead"><div class="_catDetailIdentity"><span class="_catDetailName"></span><span class="_catSkinRarity _catDetailRarity"></span></div></div><div class="_catDetailCanvasWrap"><canvas class="_catDetailCanvas" width="224" height="224"></canvas></div><div class="_catDetailDescription"></div><button class="_catReleaseButton" type="button">放生这只猫咪</button><div class="_catBuiltInHint" hidden>内置猫咪会一直守在衣柜里</div>';
    document.body.appendChild(skinDetail);

    var reveal = document.createElement('div');
    reveal.id = '_catReveal';
    reveal.hidden = true;
    reveal.setAttribute('role', 'dialog');
    reveal.setAttribute('aria-label', '猫咪开箱结果');
    reveal.innerHTML = '<div class="_catRevealCard"><button class="_catRevealClose" type="button" aria-label="关闭结果">×</button><div class="_catRevealContent"><div class="_catRevealRarity"></div><div class="_catRevealTitle"></div><canvas class="_catRevealCanvas" width="192" height="192"></canvas><p class="_catRevealHint">新皮肤已收入衣柜，并已自动装备</p></div></div>';
    document.body.appendChild(reveal);

    var lab = document.createElement('div');
    lab.id = '_catLab';
    lab.hidden = true;
    lab.setAttribute('role', 'dialog');
    lab.setAttribute('aria-label', '管理员猫咪生成实验台');
    lab.innerHTML = '<div class="_catLabPanel"><div class="_catLabHead"><div><div class="_catLabTitle">自由设计实验台</div><div class="_catLabNote">仅管理员可用 · 结果不写入衣柜 · 不消耗每日礼盒机会</div></div><button class="_catLabClose" type="button" aria-label="关闭实验台">×</button></div><div class="_catLabControls"><label class="_catLabField">稀有度<select class="_catLabType"></select></label><label class="_catLabField">猫名称<input class="_catLabNameInput" placeholder="例如：张雪峰猫" maxlength="10"></label><button class="_catLabGenerate" type="button">生成测试猫</button></div><div class="_catTrendStatus"><span class="_catTrendDot"></span><span class="_catTrendText">热点库状态读取中…</span><button class="_catTrendRefresh" type="button">立即刷新热点</button></div><div class="_catLabResult"><canvas class="_catLabCanvas" width="256" height="256"></canvas><div class="_catLabMeta"><span class="_catLabBadge">等待生成</span><div class="_catLabName">经典黑猫母版</div><div class="_catLabStats"><div class="_catLabStat _catLabSeedOut">Seed：—</div><div class="_catLabStat _catLabOps">坐标操作：—</div><div class="_catLabStat _catLabPersist">写入衣柜：否</div><div class="_catLabStat _catLabChance">消耗机会：否</div></div><pre class="_catLabJson">管理员生成后将在这里显示设计基因和坐标操作。</pre></div></div></div>';
    document.body.appendChild(lab);

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

    var activeSkin = CLASSIC_SKIN;
    var wardrobeState = { is_admin: false, can_open: false, loading: true };
    var wardrobePage = 0;
    var SKINS_PER_PAGE = 10;
    var detailSkin = null;
    var detailFrame = 0;
    var detailTimer = 0;
    var detailHideTimer = 0;
    var releaseInProgress = false;
    var detailAnchor = null;
    var giftButton = wardrobe.querySelector('._catGiftButton');
    var labButton = wardrobe.querySelector('._catLabButton');
    var trendText = lab.querySelector('._catTrendText');
    var trendDot = lab.querySelector('._catTrendDot');
    var trendRefresh = lab.querySelector('._catTrendRefresh');
    var giftStatus = wardrobe.querySelector('._catGiftStatus');
    var dailyTaskText = wardrobe.querySelector('._catDailyTask');
    var labType = lab.querySelector('._catLabType');
    var labName = lab.querySelector('._catLabNameInput');
    var labGenerate = lab.querySelector('._catLabGenerate');
    var labCanvas = lab.querySelector('._catLabCanvas');
    var labCtx = labCanvas.getContext('2d');
    labCtx.imageSmoothingEnabled = false;
    var labSkin = CLASSIC_SKIN;
    var labTimer = 0;
    var labConfigLoading = false;
    var revealRarity = reveal.querySelector('._catRevealRarity');
    var revealTitle = reveal.querySelector('._catRevealTitle');
    var revealCanvas = reveal.querySelector('._catRevealCanvas');
    var revealCtx = revealCanvas.getContext('2d');
    revealCtx.imageSmoothingEnabled = false;
    var revealSkin = null;
    var revealFrame = 0;
    var revealTimer = 0;
    var detailCanvas = skinDetail.querySelector('._catDetailCanvas');
    var detailCtx = detailCanvas.getContext('2d');
    detailCtx.imageSmoothingEnabled = false;

    function rarityClass(skin) {
      return skin.id === 'classic-black' ? 'default' : (skin.rarity || 'common');
    }
    function showShortMessage(message, isError) {
      giftStatus.textContent = message || '';
      giftStatus.classList.toggle('is-error', !!isError);
      giftStatus.classList.toggle('has-message', !!message);
      if (message) window.setTimeout(function () { if (!wardrobeState.loading && giftStatus.textContent === message) { giftStatus.textContent = ''; giftStatus.classList.remove('has-message'); } }, 2600);
    }
    function updateGiftUi(message, isError) {
      if (message) showShortMessage(message, isError);
      var task = wardrobeState.daily_task || {};
      dailyTaskText.textContent = '每日任务：' + (task.label || '完成一次飞书任务 Agent 生成任务') + (task.completed ? '（已完成）' : '');
      dailyTaskText.classList.toggle('is-complete', !!task.completed);
      giftButton.classList.toggle('is-spinning', !!wardrobeState.loading);
      giftButton.disabled = !!wardrobeState.loading;
      var title = '打开礼盒';
      if (wardrobeState.loading) title = '正在制作';
      else if (!wardrobeState.is_admin && (wardrobeState.opens_today || 0) >= 2) title = '今日领取猫咪已达上限';
      else if (!wardrobeState.is_admin && (wardrobeState.opens_today || 0) === 1 && !task.completed) title = '完成每日任务后可再次领取猫咪';
      giftButton.title = title;
      giftButton.setAttribute('aria-label', title);
    }
    function closeReveal() {
      reveal.hidden = true;
      if (revealTimer) { clearInterval(revealTimer); revealTimer = 0; }
    }
    function drawReveal() {
      if (!revealSkin) return;
      revealCtx.clearRect(0, 0, 192, 192);
      drawSprite(revealCtx, revealSkin, 0, 0, revealFrame, 12);
      if (revealSkin.effect && revealSkin.effect !== 'none') {
        revealCtx.globalAlpha = .55;
        revealCtx.fillStyle = revealSkin.palette.A || revealSkin.palette.S || revealSkin.palette.O;
        var t = Date.now() / 260;
        for (var i = 0; i < 5; i++) {
          var px = (Math.sin(t + i * 1.7) * 70 + 96) | 0;
          var py = (Math.cos(t * .8 + i) * 70 + 96) | 0;
          revealCtx.fillRect(px, py, 3, 3);
        }
        revealCtx.globalAlpha = 1;
      }
    }
    function showReveal(skin) {
      revealSkin = skin;
      revealFrame = 0;
      revealRarity.className = '_catRevealRarity ' + (skin.rarity || 'common');
      revealRarity.textContent = skin.rarity_label || skin.rarity || '普通';
      revealTitle.textContent = skin.name || '新猫咪';
      reveal.hidden = false;
      drawReveal();
      revealTimer = setInterval(function () { revealFrame = revealFrame ? 0 : 1; drawReveal(); }, 240);
    }

    function drawSprite(targetCtx, skin, x, y, legFrame, scale) {
      var rows = skin.frames && (legFrame ? skin.frames.b : skin.frames.a);
      if (!rows || rows.length !== 16) rows = CLASSIC_SKIN.frames.a;
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

    function drawDetail() {
      if (!detailSkin || skinDetail.hidden) return;
      detailCtx.clearRect(0, 0, 224, 224);
      var now = Date.now();
      var walkFrame = Math.floor(now / 190) % 2;
      var bob = walkFrame ? 1 : 0;
      var x = 16 + Math.round(Math.sin(now / 700) * 3);
      var y = 10 - bob;
      drawSprite(detailCtx, detailSkin, x, y, walkFrame, 12);
      detailCtx.fillStyle = 'rgba(100,116,139,.42)';
      detailCtx.fillRect(18, 207, 188, 2);
      detailCtx.fillRect(36 + ((now / 35) % 24), 214, 17, 2);
      detailCtx.fillRect(112 + ((now / 45) % 30), 218, 13, 2);
      drawDetailEffect(now);
    }

    function drawDetailEffect(now) {
      if (!detailSkin || !detailSkin.effect || detailSkin.effect === 'none') return;
      var effect = detailSkin.effect;
      var color = detailSkin.palette.A || detailSkin.palette.S || detailSkin.palette.I || detailSkin.palette.O;
      detailCtx.fillStyle = color;
      detailCtx.globalAlpha = .72;
      for (var i = 0; i < 7; i++) {
        var age = (now / 18 + i * 31) % 150;
        var px = 40 + (i % 3) * 17 - age * .18;
        var py = 153 - (i % 4) * 15 + Math.sin(now / 180 + i) * 8;
        if (effect === 'star' || effect === 'halo') {
          detailCtx.fillRect(Math.round(px) - 4, Math.round(py), 9, 2);
          detailCtx.fillRect(Math.round(px), Math.round(py) - 4, 2, 9);
        } else if (effect === 'web') {
          detailCtx.fillRect(Math.round(px), Math.round(py), 10, 2);
          detailCtx.fillRect(Math.round(px) + 4, Math.round(py) - 4, 2, 10);
        } else if (effect === 'royal') {
          detailCtx.fillRect(Math.round(px), Math.round(py), 6, 6);
          detailCtx.fillRect(Math.round(px) - 3, Math.round(py) - 3, 3, 3);
        } else {
          detailCtx.fillRect(Math.round(px), Math.round(py), 4, 4);
        }
      }
      detailCtx.globalAlpha = 1;
    }

    function positionSkinDetail(anchor) {
      if (!anchor || skinDetail.hidden) return;
      var rect = anchor.getBoundingClientRect();
      var width = Math.min(280, window.innerWidth - 24);
      var height = skinDetail.offsetHeight || 340;
      var left = rect.right + 10;
      if (left + width > window.innerWidth - 12) left = rect.left - width - 10;
      left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
      var top = Math.max(12, Math.min(rect.top - 30, window.innerHeight - height - 12));
      skinDetail.style.width = width + 'px';
      skinDetail.style.left = left + 'px';
      skinDetail.style.top = top + 'px';
    }

    function closeSkinDetail() {
      if (releaseInProgress) return;
      if (detailHideTimer) { clearTimeout(detailHideTimer); detailHideTimer = 0; }
      skinDetail.hidden = true;
      detailSkin = null;
      detailAnchor = null;
      if (detailTimer) { clearInterval(detailTimer); detailTimer = 0; }
    }

    function scheduleCloseSkinDetail() {
      if (releaseInProgress) return;
      if (detailHideTimer) clearTimeout(detailHideTimer);
      detailHideTimer = setTimeout(closeSkinDetail, 180);
    }

    function showSkinDetail(skin, anchor) {
      if (detailHideTimer) { clearTimeout(detailHideTimer); detailHideTimer = 0; }
      detailSkin = skin;
      detailAnchor = anchor;
      skinDetail.querySelector('._catDetailName').textContent = skin.name || '猫咪';
      var detailRarity = skinDetail.querySelector('._catDetailRarity');
      detailRarity.className = '_catSkinRarity _catDetailRarity ' + rarityClass(skin);
      detailRarity.textContent = skin.rarity_label || skin.rarity || '普通';
      skinDetail.querySelector('._catDetailDescription').textContent = skin.description || '一只住在衣柜里的像素猫。';
      var releaseButton = skinDetail.querySelector('._catReleaseButton');
      var canRelease = skin.releasable === true;
      releaseButton.hidden = !canRelease;
      skinDetail.querySelector('._catBuiltInHint').hidden = canRelease;
      skinDetail.hidden = false;
      positionSkinDetail(anchor);
      drawDetail();
      if (!detailTimer) detailTimer = setInterval(drawDetail, 80);
    }

    function renderWardrobe() {
      var grid = wardrobe.querySelector('._catSkinGrid');
      var pager = wardrobe.querySelector('._catPager');
      var pageCount = Math.max(1, Math.ceil(SKINS.length / SKINS_PER_PAGE));
      wardrobePage = Math.max(0, Math.min(wardrobePage, pageCount - 1));
      var pageSkins = SKINS.slice(wardrobePage * SKINS_PER_PAGE, (wardrobePage + 1) * SKINS_PER_PAGE);
      grid.textContent = '';
      pageSkins.forEach(function (skin) {
        var card = document.createElement('button');
        card.type = 'button';
        card.className = '_catSkinCard' + (skin.id === activeSkin.id ? ' is-active' : '');
        card.setAttribute('data-skin-id', skin.id);
        card.setAttribute('aria-pressed', skin.id === activeSkin.id ? 'true' : 'false');
        card.setAttribute('aria-label', (skin.name || '猫咪') + '，悬停查看详情，点击装备');
        card.innerHTML = '<span class="_catSkinPreview"><canvas width="48" height="48"></canvas></span><span><span class="_catSkinName"></span><span class="_catSkinRarity ' + rarityClass(skin) + '"></span></span>';
        card.querySelector('._catSkinName').textContent = skin.name;
        card.querySelector('._catSkinRarity').textContent = skin.rarity_label || skin.rarity || '普通';
        var preview = card.querySelector('canvas').getContext('2d');
        preview.imageSmoothingEnabled = false;
        drawSprite(preview, skin, 0, 0, 0, 3);
        card.addEventListener('mouseenter', function () { showSkinDetail(skin, card); });
        card.addEventListener('mouseleave', scheduleCloseSkinDetail);
        card.addEventListener('focus', function () { showSkinDetail(skin, card); });
        card.addEventListener('blur', scheduleCloseSkinDetail);
        card.addEventListener('click', function (e) {
          e.stopPropagation();
          fetch('/api/cat/equip', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({skin_id:skin.id}) })
            .then(function (r) { return r.json().then(function (data) { return { ok:r.ok, data:data }; }); })
            .then(function (result) {
              if (!result.ok) throw new Error(result.data.error || '切换皮肤失败');
              activeSkin = skin;
              localStorage.setItem(SKIN_KEY, skin.id);
              closeSkinDetail();
              renderWardrobe();
              draw();
            }).catch(function (err) { updateGiftUi(err.message || '切换皮肤失败', true); });
        });
        grid.appendChild(card);
      });
      pager.hidden = SKINS.length <= SKINS_PER_PAGE;
      pager.querySelector('._catPageText').textContent = (wardrobePage + 1) + ' / ' + pageCount;
      pager.querySelector('._catPagePrev').disabled = wardrobePage === 0;
      pager.querySelector('._catPageNext').disabled = wardrobePage >= pageCount - 1;
    }

    function drawLab() {
      if (!labSkin || lab.hidden) return;
      labCtx.clearRect(0, 0, 256, 256);
      var frameNo = Math.floor(Date.now() / 220) % 2;
      drawSprite(labCtx, labSkin, 0, 0, frameNo, 16);
    }
    function closeLab() {
      lab.hidden = true;
      if (labTimer) { clearInterval(labTimer); labTimer = 0; }
    }
    function loadTrendStatus() {
      if (!wardrobeState.is_admin) return Promise.resolve();
      return fetch('/api/cat/concepts/status', { credentials:'same-origin' })
        .then(function (r) { return r.json().then(function (data) { return {ok:r.ok,data:data}; }); })
        .then(function (result) {
          if (!result.ok) throw new Error(result.data.error || '热点状态读取失败');
          var data = result.data;
          var healthy = !!data.last_success_at;
          trendDot.classList.toggle('is-ok', healthy);
          trendText.textContent = '长期概念 ' + data.seed_count + ' · 热点 ' + data.hot_count + ' · ' + (healthy ? ('上次成功 ' + data.last_success_at) : (data.configured ? '等待首次更新' : '未配置抖音凭证/热点源'));
          trendRefresh.title = data.last_error || '';
        }).catch(function (err) {
          trendDot.classList.remove('is-ok');
          trendText.textContent = err.message || '热点状态读取失败';
        });
    }
    function openLab() {
      if (!wardrobeState.is_admin) return;
      lab.hidden = false;
      loadTrendStatus();
      drawLab();
      if (!labTimer) labTimer = setInterval(drawLab, 80);
      if (!labType.options.length && !labConfigLoading) {
        labConfigLoading = true;
        labGenerate.disabled = true;
        labGenerate.textContent = '加载实验配置…';
        fetch('/api/cat/experiment/config', { credentials:'same-origin' })
          .then(function (r) { return r.json().then(function (data) { return {ok:r.ok,data:data}; }); })
          .then(function (result) {
            if (!result.ok) throw new Error(result.data.error || '实验台读取失败');
            labType.textContent = '';
            result.data.rarities.forEach(function (item) {
              var option = document.createElement('option'); option.value = item.value; option.textContent = item.label; labType.appendChild(option);
            });
          }).catch(function (err) { closeLab(); showShortMessage(err.message || '实验台读取失败', true); })
          .finally(function () {
            labConfigLoading = false;
            labGenerate.disabled = false;
            labGenerate.textContent = '生成测试猫';
          });
      }
    }
    function renderLabResult(result) {
      labSkin = result.skin || CLASSIC_SKIN;
      var errors = result.validation && result.validation.errors || [];
      var badge = lab.querySelector('._catLabBadge');
      badge.textContent = errors.length ? '校验失败' : '校验通过';
      badge.classList.toggle('is-error', !!errors.length);
      lab.querySelector('._catLabName').textContent = labSkin.name || '实验猫';
      lab.querySelector('._catLabSeedOut').textContent = 'Seed：' + result.seed;
      lab.querySelector('._catLabOps').textContent = '坐标操作：' + ((result.operations.pattern || []).length + (result.operations.accessory || []).length);
      lab.querySelector('._catLabPersist').textContent = '写入衣柜：' + (result.persisted ? '是' : '否');
      lab.querySelector('._catLabChance').textContent = '消耗机会：' + (result.consumed_daily_chance ? '是' : '否');
      lab.querySelector('._catLabJson').textContent = JSON.stringify({design_gene:result.design_gene,operations:result.operations,validation:result.validation}, null, 2);
      drawLab();
    }

    function loadWardrobe() {
      wardrobeState.loading = true;
      updateGiftUi();
      return fetch('/api/cat/wardrobe', { credentials:'same-origin' })
        .then(function (r) { return r.json().then(function (data) { return { ok:r.ok, data:data }; }); })
        .then(function (result) {
          if (!result.ok) throw new Error(result.data.error || '衣柜读取失败');
          wardrobeState = result.data;
          wardrobeState.loading = false;
          labButton.classList.toggle('is-visible', !!wardrobeState.is_admin);
          SKINS = result.data.skins && result.data.skins.length ? result.data.skins : [CLASSIC_SKIN];
          activeSkin = SKINS.filter(function (s) { return s.id === result.data.equipped_skin_id; })[0] || SKINS[0];
          localStorage.setItem(SKIN_KEY, activeSkin.id);
          renderWardrobe();
          updateGiftUi();
          draw();
        }).catch(function (err) {
          wardrobeState = { is_admin:false, can_open:false, loading:false };
          labButton.classList.remove('is-visible');
          SKINS = [CLASSIC_SKIN]; activeSkin = CLASSIC_SKIN;
          renderWardrobe(); updateGiftUi('衣柜暂时不可用，仍可使用经典黑猫', true); draw();
          console.warn('[cat wardrobe]', err);
        });
    }

    labButton.addEventListener('click', function (e) { e.stopPropagation(); openLab(); });
    trendRefresh.addEventListener('click', function () {
      if (trendRefresh.disabled) return;
      trendRefresh.disabled = true; trendRefresh.textContent = '刷新中…';
      fetch('/api/cat/concepts/refresh', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
        .then(function (r) { return r.json().then(function (data) { return {ok:r.ok,data:data}; }); })
        .then(function (result) { if (!result.ok) throw new Error(result.data.error || '刷新失败'); return loadTrendStatus(); })
        .catch(function (err) { trendDot.classList.remove('is-ok'); trendText.textContent = err.message || '刷新失败'; })
        .finally(function () { trendRefresh.disabled = false; trendRefresh.textContent = '立即刷新热点'; });
    });
    lab.querySelector('._catLabClose').addEventListener('click', closeLab);
    lab.addEventListener('click', function (e) { if (e.target === lab) closeLab(); });
    labGenerate.addEventListener('click', function () {
      if (!wardrobeState.is_admin || labGenerate.disabled) return;
      var name = labName.value.trim();
      if (!name) {
        lab.querySelector('._catLabBadge').textContent = '请先输入猫名称';
        lab.querySelector('._catLabBadge').classList.add('is-error');
        return;
      }
      labGenerate.disabled = true; labGenerate.textContent = '生成中…';
      fetch('/api/cat/experiment/generate', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rarity:labType.value || 'common',name:name})})
        .then(function (r) { return r.json().then(function (data) { return {ok:r.ok,data:data}; }); })
        .then(function (result) { if (!result.ok) throw new Error(result.data.error || (result.data.validation && result.data.validation.errors || []).join('；') || '实验生成失败'); renderLabResult(result.data); })
        .catch(function (err) { lab.querySelector('._catLabBadge').textContent = '生成失败'; lab.querySelector('._catLabBadge').classList.add('is-error'); lab.querySelector('._catLabJson').textContent = err.message || '实验生成失败'; })
        .finally(function () { labGenerate.disabled = false; labGenerate.textContent = '生成测试猫'; });
    });

    giftButton.addEventListener('click', function (e) {
      e.stopPropagation();
      if (wardrobeState.loading) return;
      wardrobeState.loading = true;
      updateGiftUi();
      fetch('/api/cat/open', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' })
        .then(function (r) { return r.json().then(function (data) { return { ok:r.ok, data:data }; }); })
        .then(function (result) {
          wardrobeState.loading = false;
          if (!result.ok) {
            if (result.data.code === 'daily_limit' || result.data.code === 'daily_task_required') wardrobeState.can_open = false;
            throw new Error(result.data.error || '猫咪生成失败');
          }
          wardrobeState.can_open = result.data.can_open;
          wardrobeState.opens_today = result.data.opens_today;
          wardrobeState.daily_limit = result.data.daily_limit;
          wardrobeState.daily_task = result.data.daily_task || wardrobeState.daily_task;
          SKINS.push(result.data.skin);
          wardrobePage = Math.max(0, Math.ceil(SKINS.length / SKINS_PER_PAGE) - 1);
          activeSkin = result.data.skin;
          localStorage.setItem(SKIN_KEY, activeSkin.id);
          renderWardrobe();
          updateGiftUi();
          draw();
          showReveal(result.data.skin);
        }).catch(function (err) {
          wardrobeState.loading = false;
          showShortMessage(err.message || '生成失败，本次机会未消耗', true);
          updateGiftUi();
        });
    });

    reveal.querySelector('._catRevealClose').addEventListener('click', closeReveal);
    reveal.addEventListener('click', function (e) { if (e.target === reveal) closeReveal(); });

    wardrobe.querySelector('._catPagePrev').addEventListener('click', function (e) {
      e.stopPropagation();
      closeSkinDetail();
      wardrobePage = Math.max(0, wardrobePage - 1);
      renderWardrobe();
    });
    wardrobe.querySelector('._catPageNext').addEventListener('click', function (e) {
      e.stopPropagation();
      closeSkinDetail();
      wardrobePage++;
      renderWardrobe();
    });
    skinDetail.addEventListener('mouseenter', function () {
      if (detailHideTimer) { clearTimeout(detailHideTimer); detailHideTimer = 0; }
    });
    skinDetail.addEventListener('mouseleave', scheduleCloseSkinDetail);
    skinDetail.addEventListener('click', function (e) { e.stopPropagation(); });
    skinDetail.querySelector('._catReleaseButton').addEventListener('click', function (e) {
      e.stopPropagation();
      var skin = detailSkin;
      if (!skin || skin.releasable !== true) return;
      if (!window.confirm('确定要放生“' + (skin.name || '这只猫咪') + '”吗？放生后无法找回。')) return;
      var button = e.currentTarget;
      if (detailHideTimer) { clearTimeout(detailHideTimer); detailHideTimer = 0; }
      releaseInProgress = true;
      button.disabled = true;
      button.textContent = '正在放生…';
      var controller = typeof AbortController === 'function' ? new AbortController() : null;
      var releaseTimeout = setTimeout(function () { if (controller) controller.abort(); }, 5000);
      fetch('/api/cat/release', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        credentials:'same-origin',
        signal:controller ? controller.signal : undefined,
        body:JSON.stringify({skin_id:skin.id})
      })
        .then(function (r) { return r.json().then(function (data) { return { ok:r.ok, data:data }; }); })
        .then(function (result) {
          if (!result.ok) throw new Error(result.data.error || '放生失败');
          SKINS = SKINS.filter(function (item) { return item.id !== skin.id; });
          if (result.data.equipped_skin_id !== activeSkin.id) {
            activeSkin = SKINS.filter(function (item) { return item.id === result.data.equipped_skin_id; })[0] || CLASSIC_SKIN;
            localStorage.setItem(SKIN_KEY, activeSkin.id);
          }
          releaseInProgress = false;
          closeSkinDetail();
          renderWardrobe();
          draw();
          showShortMessage((result.data.released_skin_name || '猫咪') + '已经回到自由世界');
        }).catch(function (err) {
          var message = err && err.name === 'AbortError' ? '放生请求超时，请刷新衣柜确认结果' : (err.message || '放生失败');
          showShortMessage(message, true);
        }).finally(function () {
          clearTimeout(releaseTimeout);
          releaseInProgress = false;
          if (button && button.isConnected) {
            button.disabled = false;
            button.textContent = '放生这只猫咪';
          }
        });
    });

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
        loadWardrobe();
      } else closeSkinDetail();
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
      if (!wardrobe.hidden && !wrap.contains(e.target) && !wardrobe.contains(e.target) && !skinDetail.contains(e.target)) setWardrobeOpen(false);
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !lab.hidden) { closeLab(); return; }
      if (e.key === 'Escape' && !reveal.hidden) { closeReveal(); return; }
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
        color: activeSkin.effect === 'star'
          ? (activeSkin.palette.P || activeSkin.palette.I || activeSkin.palette.O)
          : (activeSkin.palette.A || activeSkin.palette.S || activeSkin.palette.O)
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
        var effect = activeSkin.effect;
        if ((effect === 'star' || effect === 'halo') && p.life > 10) {
          ctx.fillRect(Math.round(p.x) - 1, Math.round(p.y), 3, 1);
          ctx.fillRect(Math.round(p.x), Math.round(p.y) - 1, 1, 3);
        } else if (effect === 'web') {
          ctx.fillRect(Math.round(p.x), Math.round(p.y), 3, 1);
          ctx.fillRect(Math.round(p.x) + 1, Math.round(p.y) - 1, 1, 3);
        } else if (effect === 'royal') {
          ctx.fillRect(Math.round(p.x), Math.round(p.y), 2, 2);
          ctx.fillRect(Math.round(p.x) - 1, Math.round(p.y) - 1, 1, 1);
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
    window.addEventListener('resize', function () { refreshColors(); if (!wardrobe.hidden) positionWardrobe(); if (!skinDetail.hidden) positionSkinDetail(detailAnchor); if (state !== STATE.PLAY) draw(); });

    renderWardrobe();
    draw();
    loadWardrobe();
  }

  if (document.querySelector('.portal-bar')) init();
  else document.addEventListener('DOMContentLoaded', init);
})();
