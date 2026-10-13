'use strict';
/* AI 短剧自动化全流程 —— 前端。
   样式全部走 shared-ui：结构类用共享的 .ui-*，本模块只保留 .dp-* 布局类。
   接口路径必须是**不带前导斜杠**的相对路径，否则在 /drama-pipeline/ 下会打到
   Portal 自己头上（见 CLAUDE.local.md）。

   出片这一段**只连本机 MiniMax H3**（8801 的 video_local），没有任何云 provider
   入口 —— UI 里也见不到「云端」两个字。 */
(function () {
  var state = {
    config: null,
    local: null,
    runs: [],
    runId: '',
    run: null,
    stageId: '',
    artifact: '',
    artifactDirty: false,
    shots: [],
    shotsParsed: true,
    shotFiles: {},          // { 镜号: [{data_url, filename}] }
    shotsBusy: false,
    poll: null,
    shotPoll: null,
    skillsOpen: false,
  };

  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };

  var STAGE_LABEL = {
    pending: '待运行', running: '运行中', done: '已完成',
    failed: '失败', skipped: '已跳过', cancelled: '已取消',
  };
  var RUN_LABEL = {
    pending: '待运行', running: '运行中', succeeded: '已完成',
    failed: '失败', partial: '部分完成', cancelled: '已取消', interrupted: '中断（可续跑）',
  };
  var SHOT_LABEL = {
    idle: '未出片', submitted: '已排队', pending: '排队中', running: '本地生成中',
    querying: '本地生成中', succeeded: '已完成', failed: '失败', cancelled: '已取消',
  };
  var BADGE = {
    pending: 'ui-badge--neutral', running: 'ui-badge--info', done: 'ui-badge--success',
    succeeded: 'ui-badge--success', failed: 'ui-badge--danger', cancelled: 'ui-badge--neutral',
    skipped: 'ui-badge--neutral', partial: 'ui-badge--warning', interrupted: 'ui-badge--warning',
    idle: 'ui-badge--neutral', submitted: 'ui-badge--info', querying: 'ui-badge--info',
  };
  var SHOT_ACTIVE = { submitted: 1, pending: 1, running: 1, querying: 1 };

  function badge(status, label) {
    return '<span class="ui-badge ' + (BADGE[status] || 'ui-badge--neutral') + '">' +
      esc(label || status || '') + '</span>';
  }

  function api(path, opts) {
    opts = opts || {};
    return fetch(path, {
      method: opts.method || 'GET',
      headers: opts.body ? { 'Content-Type': 'application/json' } : {},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      credentials: 'same-origin',
    }).then(function (r) {
      return r.text().then(function (t) {
        var data;
        try { data = t ? JSON.parse(t) : {}; } catch (e) { data = { ok: false, error: t.slice(0, 300) }; }
        if (!r.ok && data && !data.error) data.error = 'HTTP ' + r.status;
        return data;
      });
    });
  }

  // ---------- 配置 / 本地能力 ----------

  function loadConfig() {
    return api('api/config').then(function (res) {
      if (!res.ok) {
        var pill = $('dp-llm');
        pill.className = 'ui-badge ui-badge--danger';
        pill.textContent = '配置读取失败';
        return;
      }
      state.config = res;
      var llm = res.llm || {};
      var el = $('dp-llm');
      if (llm.has_key) {
        el.className = 'ui-badge ui-badge--success';
        el.textContent = '文本：' + (llm.provider || '');
      } else {
        el.className = 'ui-badge ui-badge--danger';
        el.textContent = '文本：未找到 API Key';
      }
      $('dp-mode').innerHTML = (res.modes || []).map(function (m) {
        return '<option value="' + esc(m.id) + '">' + esc(m.label) + '</option>';
      }).join('');
      $('dp-stage-checks').innerHTML = (res.stages || []).map(function (s) {
        return '<label><input type="checkbox" checked data-stage="' + esc(s.id) + '">' +
          '<span>' + esc(s.order + '. ' + s.title) + (s.optional ? '（可选）' : '') + '</span></label>';
      }).join('');
      renderSkills();
    });
  }

  function loadLocal() {
    return api('api/local').then(function (res) {
      if (!res.ok || !res.local_h3) return;
      state.local = res.local_h3;
      renderLocalBadge();
    });
  }

  function renderLocalBadge() {
    var el = $('dp-local');
    if (!el || !state.local) return;
    var info = state.local;
    if (info.ready) {
      el.className = 'ui-badge ui-badge--success';
      el.textContent = '本地出片：MiniMax H3 就绪 · 云费用 ¥0' +
        (info.queue ? '（队列 ' + info.queue + '）' : '');
      el.title = info.base_url + ' · provider=' + info.provider;
    } else {
      el.className = 'ui-badge ui-badge--danger';
      el.textContent = '本地出片：不可用';
      el.title = info.error || '';
    }
  }

  function renderSkills() {
    var skills = (state.config && state.config.skills) || [];
    $('dp-skills-count').textContent = '共 ' + skills.length + ' 份';
    $('dp-skills').innerHTML = skills.map(function (s) {
      var tags = (s.stages && s.stages.length)
        ? s.stages.map(function (id) { return '<span class="ui-chip">' + esc(id) + '</span>'; }).join('')
        : '<span class="ui-chip">旁路 / 工具</span>';
      return '<div class="dp-skill">' +
        '<div class="dp-skill__title">' + esc(s.title) +
          (s.exists ? '' : ' ' + badge('failed', '文件缺失')) + '</div>' +
        '<div class="ui-caption">' + esc(s.role) + '</div>' +
        '<div class="ui-caption dp-skill__path">' + esc(s.file) + '</div>' +
        '<div class="dp-tags">' + tags + '</div>' +
        '</div>';
    }).join('') || '<div class="ui-empty ui-empty--compact">技能索引为空</div>';
  }

  // ---------- 运行列表 ----------

  function loadRuns() {
    return api('api/runs').then(function (res) {
      state.runs = (res && res.runs) || [];
      renderRuns();
    });
  }

  function renderRuns() {
    if (!state.runs.length) {
      $('dp-runs').innerHTML = '<div class="ui-empty ui-empty--compact">暂无运行</div>';
      return;
    }
    $('dp-runs').innerHTML = state.runs.map(function (r) {
      var ts = r.created_at ? new Date(r.created_at * 1000).toLocaleString() : '';
      return '<div class="dp-run' + (r.id === state.runId ? ' is-active' : '') +
        '" data-run="' + esc(r.id) + '">' +
        '<div class="dp-run__topic">' + esc(r.topic || '(无主题)') + '</div>' +
        '<div class="dp-run__meta"><span>' + esc(RUN_LABEL[r.status] || r.status) +
          ' · ' + r.done + '/' + r.total + '</span><span>' + esc(ts) + '</span></div>' +
        '</div>';
    }).join('');
    Array.prototype.forEach.call($('dp-runs').querySelectorAll('.dp-run'), function (el) {
      el.onclick = function () { selectRun(el.getAttribute('data-run')); };
    });
  }

  // ---------- 运行详情 ----------

  function selectRun(runId) {
    state.runId = runId;
    state.stageId = '';
    state.artifact = '';
    state.artifactDirty = false;
    state.shots = [];
    state.shotFiles = {};
    stopPoll();
    stopShotPoll();
    renderRuns();
    return refreshRun().then(function () {
      if (state.run && !state.stageId) {
        state.stageId = state.run.current_stage || firstStageWithWork(state.run) || '';
      }
      return loadArtifact();
    }).then(loadShots).then(function () {
      renderRun();
      schedulePoll();
      scheduleShotPoll();
    });
  }

  function firstStageWithWork(run) {
    var ids = run.stage_ids || [];
    for (var i = 0; i < ids.length; i++) {
      var st = (run.stages || {})[ids[i]] || {};
      if (st.status === 'done' || st.status === 'running' || st.status === 'failed') return ids[i];
    }
    return ids[0] || '';
  }

  function refreshRun() {
    if (!state.runId) return Promise.resolve();
    return api('api/jobs/' + encodeURIComponent(state.runId)).then(function (res) {
      if (res.ok) state.run = res.run;
    });
  }

  function stageById(id) {
    var cfg = state.config;
    if (!cfg) return null;
    for (var i = 0; i < cfg.stages.length; i++) if (cfg.stages[i].id === id) return cfg.stages[i];
    return null;
  }

  function currentStage() { return stageById(state.stageId); }

  function loadArtifact() {
    var stage = currentStage();
    if (!stage || !state.runId) { state.artifact = ''; return Promise.resolve(); }
    var entry = (state.run.stages || {})[stage.id] || {};
    if (entry.status !== 'done' && entry.status !== 'failed') {
      state.artifact = '';
      return Promise.resolve();
    }
    var url = 'api/jobs/' + encodeURIComponent(state.runId) + '/artifacts/' + encodeURIComponent(stage.artifact);
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
      return r.ok ? r.text() : '';
    }).then(function (t) {
      state.artifact = t;
      state.artifactDirty = false;
    });
  }

  // ---------- 出片（本地 MiniMax H3） ----------

  function loadShots() {
    if (!state.runId) { state.shots = []; return Promise.resolve(); }
    return api('api/jobs/' + encodeURIComponent(state.runId) + '/shots').then(function (res) {
      if (!res.ok) { state.shots = []; return; }
      state.shots = res.shots || [];
      state.shotsParsed = res.parsed !== false;
    });
  }

  function shotActive() {
    return state.shots.some(function (s) { return SHOT_ACTIVE[s.status]; });
  }

  function renderShots() {
    var host = $('dp-shots');
    if (!host) return;
    var local = state.local || {};
    var head =
      '<div class="ui-panel__header">' +
        '<h2 class="ui-heading">出片 · 本机 MiniMax H3</h2>' +
        '<div class="ui-toolbar__group">' +
          (local.ready ? badge('succeeded', '本地就绪') : badge('failed', '本地不可用')) +
          badge('succeeded', '云费用 ¥0') +
          '<button type="button" class="ui-btn ui-btn--secondary ui-btn--sm" id="dp-shots-refresh" ' +
            (state.shotsBusy ? 'disabled' : '') + '>刷新状态</button>' +
          '<button type="button" class="ui-btn ui-btn--primary ui-btn--sm" id="dp-shots-all">全部出片</button>' +
        '</div>' +
      '</div>';

    var body;
    if (!local.ready && local.error) {
      body = '<div class="ui-alert ui-alert--danger">本地出片不可用：' + esc(local.error) + '</div>';
    }
    if (!state.shots.length) {
      body = (body || '') + '<div class="ui-empty ui-empty--compact">' +
        '还没有镜头。先跑完第 8 阶段「视频提示词」，这里会按镜自动列出来。</div>';
    } else {
      if (!state.shotsParsed) {
        body = (body || '') + '<div class="ui-alert ui-alert--warning">' +
          '没能从第 8 阶段产物里识别出镜头结构，已把整篇当成一个镜头。' +
          '你可以直接在下面的提示词框里改成要出片的那一段。</div>';
      }
      body = (body || '') + '<div class="dp-shots">' + state.shots.map(shotCard).join('') + '</div>';
    }
    host.innerHTML = head + '<div class="ui-panel__body">' + body + '</div>';
    bindShotEvents(host);
  }

  function shotCard(shot) {
    var index = shot.index;
    var active = !!SHOT_ACTIVE[shot.status];
    var local = state.local || {};
    var running = shot.status === 'running' || shot.status === 'querying' || shot.status === 'submitted' || shot.status === 'pending';

    var modes = ['', 't2v', 'i2v', 'flf2v', 'ref2v'];
    var modeLabels = (local.mode_labels) || {};
    var modeSel = modes.map(function (m) {
      var label = m ? (m + ' · ' + (modeLabels[m] || '')) : '自动（按参考图数）';
      var sel = (shot.mode || '') === m ? ' selected' : '';
      return '<option value="' + esc(m) + '"' + sel + '>' + esc(label) + '</option>';
    }).join('');

    var durations = (local.durations || []).map(function (d) {
      return '<option value="' + esc(d.id) + '">' + esc(d.label) + '</option>';
    }).join('');
    var ratios = (local.ratios || []).map(function (r) {
      return '<option value="' + esc(r.id) + '">' + esc(r.label) + '</option>';
    }).join('');

    var params = shot.params || {};
    var meta = [];
    if (shot.mode) meta.push(shot.mode_label || shot.mode);
    if (shot.images) meta.push('参考图 ' + shot.images + ' 张');
    if (params.num_frames) meta.push(params.num_frames + ' 帧');
    if (params.h3_aspect_ratio) meta.push(String(params.h3_aspect_ratio).split(' ')[0]);
    if (params.steps) meta.push(params.steps + ' 步');
    if (params.attention_mode) meta.push(params.attention_mode);
    if (shot.elapsed) meta.push(shot.elapsed + ' 秒');
    if (shot.video_bytes) meta.push((shot.video_bytes / 1048576).toFixed(1) + ' MB');
    if (shot.seed) meta.push('seed ' + shot.seed);

    var video = shot.has_video
      ? '<video class="dp-shot__video" controls preload="metadata" muted playsinline src="' +
        'api/jobs/' + encodeURIComponent(state.runId) + '/shots/' + index + '/video' +
        '?t=' + (shot.updated_at || Date.now()) + '"></video>'
      : '';

    return '<div class="dp-shot" data-shot="' + index + '">' +
      '<div class="dp-shot__head">' +
        '<span class="dp-shot__no">镜 ' + index + '</span>' +
        '<span class="dp-shot__title">' + esc(shot.title || '') + '</span>' +
        badge(shot.status || 'idle', SHOT_LABEL[shot.status] || shot.status || '') +
      '</div>' +
      (meta.length ? '<div class="ui-caption">' + esc(meta.join(' · ')) + '</div>' : '') +
      '<textarea class="ui-textarea dp-shot__prompt" spellcheck="false" rows="4">' +
        esc(shot.prompt_override || shot.prompt || '') + '</textarea>' +
      (shot.error ? '<div class="ui-alert ui-alert--danger dp-mt">' + esc(shot.error) + '</div>' : '') +
      '<div class="dp-row dp-mt">' +
        '<select class="ui-select dp-select--sm dp-shot__mode">' + modeSel + '</select>' +
        '<select class="ui-select dp-select--sm dp-shot__duration"><option value="">时长：自动</option>' +
          durations + '</select>' +
        '<select class="ui-select dp-select--sm dp-shot__ratio"><option value="">画幅：自动</option>' +
          ratios + '</select>' +
        '<label class="ui-btn ui-btn--secondary ui-btn--sm dp-shot__pick">加参考图' +
          '<input type="file" accept="image/*" multiple hidden></label>' +
        '<button type="button" class="ui-btn ui-btn--primary ui-btn--sm" data-act="generate"' +
          (running ? ' disabled' : '') + '>' + (shot.has_video ? '重新出片' : '出片') + '</button>' +
        (running ? '<button type="button" class="ui-btn ui-btn--secondary ui-btn--sm" data-act="cancel">取消</button>' : '') +
        '<span class="ui-caption dp-shot__files">' +
          (state.shotFiles[index] ? ('待上传 ' + state.shotFiles[index].length + ' 张') : '') + '</span>' +
      '</div>' +
      video +
      '</div>';
  }

  function bindShotEvents(host) {
    Array.prototype.forEach.call(host.querySelectorAll('.dp-shot'), function (card) {
      var index = parseInt(card.getAttribute('data-shot'), 10);
      var fileInput = card.querySelector('.dp-shot__pick input[type=file]');
      if (fileInput) {
        fileInput.onchange = function () {
          readImageFiles(index, fileInput.files);
        };
      }
      Array.prototype.forEach.call(card.querySelectorAll('[data-act]'), function (btn) {
        btn.onclick = function () {
          if (btn.getAttribute('data-act') === 'generate') generateShot(index, card);
          else cancelShot(index);
        };
      });
    });
    var refresh = $('dp-shots-refresh');
    if (refresh) refresh.onclick = function () { refreshShots(); };
    var all = $('dp-shots-all');
    if (all) all.onclick = function () { generateAll(); };
  }

  function readImageFiles(index, files) {
    var list = Array.prototype.slice.call(files || []).slice(0, 9);
    if (!list.length) return;
    Promise.all(list.map(function (file) {
      return new Promise(function (resolve) {
        var reader = new FileReader();
        reader.onload = function () {
          resolve({ data_url: String(reader.result || ''), filename: file.name });
        };
        reader.onerror = function () { resolve(null); };
        reader.readAsDataURL(file);
      });
    })).then(function (items) {
      state.shotFiles[index] = items.filter(Boolean);
      renderShots();
    });
  }

  function generateShot(index, card) {
    if (!state.runId) return;
    var body = {};
    if (card) {
      var ta = card.querySelector('.dp-shot__prompt');
      if (ta) body.prompt = ta.value;
      var mode = card.querySelector('.dp-shot__mode');
      if (mode) body.mode = mode.value;
      var duration = card.querySelector('.dp-shot__duration');
      if (duration) body.duration_id = duration.value;
      var ratio = card.querySelector('.dp-shot__ratio');
      if (ratio) body.ratio = ratio.value;
    }
    if (state.shotFiles[index] && state.shotFiles[index].length) {
      body.images = state.shotFiles[index];
    }
    state.shotsBusy = true;
    api('api/jobs/' + state.runId + '/shots/' + index + '/generate', { method: 'POST', body: body })
      .then(function (res) {
        state.shotsBusy = false;
        if (!res.ok) { alert(res.error || '提交失败'); return; }
        state.shotFiles[index] = null;
        return refreshShots();
      })
      .catch(function (e) { state.shotsBusy = false; alert(String(e)); });
  }

  function generateAll() {
    var pending = state.shots.filter(function (s) { return !SHOT_ACTIVE[s.status]; });
    if (!pending.length) { alert('没有待出片的镜头'); return; }
    if (!confirm('把 ' + pending.length + ' 个镜头依次提交到本机 MiniMax H3？\n' +
                 '按每条 4 秒约 2~3 分钟估算，总耗时较长，但云费用为 0。')) return;
    var chain = Promise.resolve();
    pending.forEach(function (shot) {
      chain = chain.then(function () {
        return api('api/jobs/' + state.runId + '/shots/' + shot.index + '/generate', {
          method: 'POST', body: {},
        });
      });
    });
    state.shotsBusy = true;
    chain.then(function () {
      state.shotsBusy = false;
      return refreshShots();
    }).catch(function (e) { state.shotsBusy = false; alert(String(e)); });
  }

  function cancelShot(index) {
    api('api/jobs/' + state.runId + '/shots/' + index + '/cancel', { method: 'POST' })
      .then(function (res) {
        if (!res.ok) alert(res.error || '取消失败');
        return refreshShots();
      });
  }

  function refreshShots() {
    if (!state.runId) return Promise.resolve();
    state.shotsBusy = true;
    return api('api/jobs/' + state.runId + '/shots/refresh', { method: 'POST' })
      .catch(function () { return null; })
      .then(function () { return loadShots(); })
      .then(function () {
        state.shotsBusy = false;
        renderShots();
        scheduleShotPoll();
      });
  }

  function scheduleShotPoll() {
    stopShotPoll();
    if (!shotActive()) return;
    state.shotPoll = setInterval(function () {
      api('api/jobs/' + state.runId + '/shots/refresh', { method: 'POST' })
        .catch(function () { return null; })
        .then(loadShots)
        .then(function () {
          renderShots();
          if (!shotActive()) stopShotPoll();
        });
    }, 5000);
  }

  function stopShotPoll() {
    if (state.shotPoll) { clearInterval(state.shotPoll); state.shotPoll = null; }
  }

  // ---------- 运行详情渲染 ----------

  function renderRun() {
    var view = $('dp-view');
    if (!state.run) {
      view.innerHTML = '<div class="ui-empty">左侧新建一次运行，或在「运行记录」里选一条</div>';
      return;
    }
    var run = state.run;

    var steps = (run.stage_ids || []).map(function (sid) {
      var s = stageById(sid) || { order: '?', title: sid };
      var st = (run.stages || {})[sid] || {};
      return '<button type="button" class="dp-step' + (sid === state.stageId ? ' is-active' : '') +
        '" data-stage="' + esc(sid) + '">' + esc(s.order + '. ' + s.title) +
        badge(st.status, STAGE_LABEL[st.status] || st.status || '') + '</button>';
    }).join('');

    var stage = currentStage();
    var entry = stage ? ((run.stages || {})[stage.id] || {}) : {};

    var actions = '<div class="dp-row">';
    if (run.status === 'failed' || run.status === 'partial' || run.status === 'interrupted') {
      actions += '<button type="button" class="ui-btn ui-btn--primary ui-btn--sm" id="dp-resume">从失败处续跑</button>';
    }
    if (run.status === 'running' || run.status === 'pending') {
      actions += '<button type="button" class="ui-btn ui-btn--secondary ui-btn--sm" id="dp-cancel">取消</button>';
    }
    if (stage) {
      actions += '<button type="button" class="ui-btn ui-btn--secondary ui-btn--sm" id="dp-rerun">重跑本阶段及之后</button>' +
        '<button type="button" class="ui-btn ui-btn--primary ui-btn--sm" id="dp-save"' +
        (state.artifact ? '' : ' disabled') + '>保存产物</button>';
    }
    actions += '</div>';

    var meta = stage
      ? '<div class="ui-caption dp-mt">产物文件名 <code>' + esc(stage.artifact) + '</code>' +
        (entry.chars ? ' · ' + entry.chars + ' 字' : '') +
        (entry.provider ? ' · ' + esc(entry.provider) + (entry.model ? '/' + esc(entry.model) : '') : '') +
        '</div>'
      : '';

    view.innerHTML =
      '<section class="ui-panel">' +
        '<div class="ui-panel__header">' +
          '<h2 class="ui-heading">' + esc(run.topic || '(无主题)') + '</h2>' +
          badge(run.status, RUN_LABEL[run.status] || run.status) +
        '</div>' +
        '<div class="ui-panel__body">' +
          '<div class="ui-caption">' + esc(run.id) + ' · ' + esc(run.mode || '') + '</div>' +
          '<div class="dp-steps">' + steps + '</div>' +
          actions +
          (entry.error ? '<div class="ui-alert ui-alert--danger dp-mt">' + esc(entry.error) + '</div>' : '') +
          meta +
          (stage
            ? '<textarea id="dp-artifact" class="ui-textarea dp-artifact dp-mt" spellcheck="false" ' +
              'placeholder="该阶段尚未产出。点「重跑本阶段及之后」生成，或直接在此手写后保存。"></textarea>'
            : '') +
        '</div>' +
      '</section>' +
      '<section class="ui-panel" id="dp-shots-panel"></section>' +
      '<section class="ui-panel">' +
        '<div class="ui-panel__header"><h2 class="ui-heading">事件</h2></div>' +
        '<div class="ui-panel__body">' +
          '<div class="dp-events"><div class="ui-job-status-card__events">' +
            esc((run.events || []).join('\n')) +
          '</div></div>' +
        '</div>' +
      '</section>';

    Array.prototype.forEach.call(view.querySelectorAll('.dp-step'), function (el) {
      el.onclick = function () {
        state.stageId = el.getAttribute('data-stage');
        loadArtifact().then(renderRun);
      };
    });

    var ta = $('dp-artifact');
    if (ta) {
      ta.value = state.artifact || '';
      ta.oninput = function () {
        state.artifactDirty = true;
        var b = $('dp-save');
        if (b) b.disabled = false;
      };
    }
    var resume = $('dp-resume');
    if (resume) resume.onclick = function () { act('api/jobs/' + state.runId + '/resume'); };
    var cancel = $('dp-cancel');
    if (cancel) cancel.onclick = function () { act('api/jobs/' + state.runId + '/cancel'); };
    var rerun = $('dp-rerun');
    if (rerun) rerun.onclick = function () {
      if (!confirm('重跑「' + (stage ? stage.title : '') + '」及之后的所有阶段？已有产物会被覆盖。')) return;
      act('api/jobs/' + state.runId + '/stages/' + state.stageId + '/rerun');
    };
    var save = $('dp-save');
    if (save) save.onclick = saveArtifact;

    // #dp-shots 由 renderShots 填，必须放在这里之后（panel 刚建出来）
    var panel = $('dp-shots-panel');
    if (panel) panel.innerHTML = '<div id="dp-shots"></div>';
    renderShots();
  }

  function act(url) {
    api(url, { method: 'POST' }).then(function (res) {
      if (!res.ok) alert(res.error || res.message || '操作失败');
      return refreshRun().then(renderRun);
    }).then(schedulePoll);
  }

  function saveArtifact() {
    var ta = $('dp-artifact');
    if (!ta) return;
    api('api/jobs/' + state.runId + '/stages/' + state.stageId + '/artifact', {
      method: 'PUT', body: { content: ta.value },
    }).then(function (res) {
      if (!res.ok) { alert(res.error || '保存失败'); return; }
      state.artifact = ta.value;
      state.artifactDirty = false;
      return refreshRun().then(renderRun);
    });
  }

  // ---------- 轮询 ----------

  function schedulePoll() {
    stopPoll();
    if (!state.run) return;
    if (state.run.status !== 'running' && state.run.status !== 'pending') return;
    state.poll = setInterval(function () {
      refreshRun().then(function () {
        if (state.run && state.artifactDirty === false) return loadArtifact();
      }).then(function () {
        return loadShots();
      }).then(function () {
        renderRun();
        renderRuns();
        if (!state.run || (state.run.status !== 'running' && state.run.status !== 'pending')) {
          stopPoll();
          loadRuns();
        }
      });
    }, 2500);
  }

  function stopPoll() {
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
  }

  // ---------- 新建运行 ----------

  function startRun() {
    var topic = $('dp-topic').value.trim();
    var err = $('dp-new-err');
    err.textContent = '';
    if (!topic) { err.textContent = '请先填写项目主题'; return; }
    var stages = [];
    Array.prototype.forEach.call($('dp-stage-checks').querySelectorAll('input[data-stage]'), function (cb) {
      if (cb.checked) stages.push(cb.getAttribute('data-stage'));
    });
    if (!stages.length) { err.textContent = '至少要选择一个阶段'; return; }
    var btn = $('dp-start');
    btn.disabled = true;
    api('api/jobs', { method: 'POST', body: { topic: topic, mode: $('dp-mode').value, stages: stages } })
      .then(function (res) {
        btn.disabled = false;
        if (!res.ok) { err.textContent = res.error || '创建失败'; return; }
        $('dp-topic').value = '';
        return loadRuns().then(function () { return selectRun(res.run_id); });
      })
      .catch(function (e) { btn.disabled = false; err.textContent = String(e); });
  }

  // ---------- 引导 ----------

  document.addEventListener('DOMContentLoaded', function () {
    $('dp-start').onclick = startRun;
    $('dp-select-all').onclick = function () {
      var boxes = $('dp-stage-checks').querySelectorAll('input[data-stage]');
      var allOn = Array.prototype.every.call(boxes, function (b) { return b.checked; });
      Array.prototype.forEach.call(boxes, function (b) { b.checked = !allOn; });
      this.textContent = allOn ? '全选' : '全不选';
    };
    $('dp-skills-btn').onclick = function () {
      state.skillsOpen = !state.skillsOpen;
      $('dp-skills-card').hidden = !state.skillsOpen;
    };
    loadConfig()
      .then(loadLocal)
      .then(loadRuns)
      .then(function () {
        if (state.runs.length) return selectRun(state.runs[0].id);
        renderRun();
      });
  });
})();
