(function () {
  if (window.__portalRagInterceptorInstalled) return;
  window.__portalRagInterceptorInstalled = true;

  const PREFLIGHT_URL = '/rag-assistant/api/rag/preflight';
  const PREFLIGHT_TIMEOUT_MS = 25000;
  let preflightInFlight = false;
  const PROMPT_KEYS = ['prompt', 'text', 'user_prompt', 'instruction'];
  const MARKER = '[飞书知识库自动补充]';
  const ENDPOINT_RE = /\/api\/(?:v1\/)?(?:jobs(?:\/json)?|virtual\/jobs|real\/jobs|projects\/[^/]+\/render|runs)/;

  function isGenerationRequest(url) {
    return ENDPOINT_RE.test(String(url || ''));
  }

  function firstPrompt(value) {
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = firstPrompt(item);
        if (found) return found;
      }
      return '';
    }
    if (value && typeof value === 'object') {
      for (const key of Object.keys(value)) {
        if (PROMPT_KEYS.includes(key.toLowerCase()) && typeof value[key] === 'string' && value[key].trim()) {
          return value[key].trim();
        }
      }
      for (const key of Object.keys(value)) {
        const found = firstPrompt(value[key]);
        if (found) return found;
      }
    }
    return '';
  }

  function readPrompt(body) {
    if (body instanceof FormData) {
      for (const key of PROMPT_KEYS) {
        const value = body.get(key);
        if (typeof value === 'string' && value.trim()) return value.trim();
      }
      return '';
    }
    if (typeof body === 'string') {
      try {
        return firstPrompt(JSON.parse(body));
      } catch (error) {
        return '';
      }
    }
    return '';
  }

  function notify(message) {
    try {
      if (window.parent && typeof window.parent.portalToast === 'function') {
        window.parent.portalToast(message, 'warning');
      } else {
        window.alert(message);
      }
    } catch (error) {
      window.alert(message);
    }
  }

  async function preflight(prompt) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), PREFLIGHT_TIMEOUT_MS);
    try {
      const response = await window.fetch(PREFLIGHT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, optimize: false }),
        signal: controller.signal,
      });
      return response.json().catch(() => ({ detected: false }));
    } finally {
      clearTimeout(timer);
    }
  }

  function showRagChoicePopup(matches) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'rag-choice-overlay';
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:99999;display:flex;align-items:center;justify-content:center;padding:20px;';

      const card = document.createElement('div');
      card.style.cssText = 'background:#fff;border-radius:10px;max-width:520px;width:100%;padding:20px;box-shadow:0 20px 60px rgba(0,0,0,.25);color:#172033;font-family:system-ui,-apple-system,Segoe UI,sans-serif;';

      const title = document.createElement('h3');
      title.textContent = '检测到飞书知识库规则';
      title.style.cssText = 'margin:0 0 10px;font-size:16px;font-weight:700;color:#b42318;';

      const desc = document.createElement('p');
      desc.textContent = '当前提示词可能违反以下规则：';
      desc.style.cssText = 'margin:0 0 10px;font-size:13px;color:#475569;';

      const list = document.createElement('ul');
      list.style.cssText = 'margin:0 0 12px;padding-left:18px;font-size:13px;line-height:1.6;color:#172033;';
      (matches || []).forEach((item) => {
        const li = document.createElement('li');
        li.style.cssText = 'margin-bottom:8px;';
        const strong = document.createElement('strong');
        strong.textContent = item.title || '未命名规则';
        const detail = document.createElement('div');
        detail.textContent = (item.content || '').slice(0, 120);
        detail.style.cssText = 'margin-top:2px;font-size:12px;color:#64748b;';
        li.append(strong, detail);
        list.appendChild(li);
      });

      const hint = document.createElement('p');
      hint.textContent = '你可以直接继续生成，也可以返回修改。返回后可点击「✨ 优化」一键更新提示词。';
      hint.style.cssText = 'margin:0 0 14px;font-size:12px;color:#64748b;';

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;';

      const backBtn = document.createElement('button');
      backBtn.type = 'button';
      backBtn.textContent = '返回修改提示词';
      backBtn.style.cssText = 'padding:8px 12px;border-radius:6px;border:1px solid #d1d5db;background:#fff;color:#172033;cursor:pointer;';

      const continueBtn = document.createElement('button');
      continueBtn.type = 'button';
      continueBtn.textContent = '继续生成';
      continueBtn.style.cssText = 'padding:8px 12px;border-radius:6px;border:1px solid #2563eb;background:#2563eb;color:#fff;cursor:pointer;';

      function close(value) {
        overlay.remove();
        resolve(value);
      }
      backBtn.addEventListener('click', () => close(false));
      continueBtn.addEventListener('click', () => close(true));
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) close(false);
      });

      actions.append(backBtn, continueBtn);
      card.append(title, desc, list, hint, actions);
      overlay.appendChild(card);
      document.body.appendChild(overlay);
    });
  }

  const originalFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    const options = init || {};
    if (!isGenerationRequest(url) || String(options.method || 'GET').toUpperCase() !== 'POST') {
      return originalFetch(input, init);
    }

    const prompt = readPrompt(options.body);
    if (!prompt || prompt.includes(MARKER)) {
      return originalFetch(input, init);
    }

    if (preflightInFlight) {
      notify('正在检查飞书知识库，请稍候。');
      return new Response(JSON.stringify({ ok: false, error: '正在检查飞书知识库', rag_review: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }

    preflightInFlight = true;
    try {
      notify('正在检查飞书知识库，请稍候。');
      const result = await preflight(prompt);
      if (!result || !result.detected) {
        return originalFetch(input, init);
      }

      const shouldContinue = await showRagChoicePopup(result.matches || []);
      if (shouldContinue) {
        return originalFetch(input, init);
      }

      return new Response(JSON.stringify({
        ok: false,
        error: '已返回修改提示词',
        rag_review: true
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      });
    } catch (error) {
      if (error && error.name === 'AbortError') {
        notify('飞书知识库检查超时，本次按原提示词继续生成。');
      }
      return originalFetch(input, init);
    } finally {
      preflightInFlight = false;
    }
  };
})();