(function () {
  if (window.__portalRagInterceptorInstalled) return;
  window.__portalRagInterceptorInstalled = true;

  const PREFLIGHT_URL = '/rag-assistant/api/rag/preflight';
  const PROMPT_KEYS = ['prompt', 'text', 'user_prompt', 'instruction'];
  const MARKER = '[飞书知识库自动补充]';
  let lastReviewedPrompt = null;
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

  function updatePromptElement(prompt) {
    const selectors = PROMPT_KEYS.flatMap((key) => [
      `textarea[name="${key}"]`,
      `input[name="${key}"]`,
    ]);
    const element = document.querySelector(selectors.join(','));
    if (!element) return false;
    element.value = prompt;
    element.dispatchEvent(new Event('input', { bubbles: true }));
    element.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
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
    const response = await window.fetch(PREFLIGHT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    return response.json().catch(() => ({ detected: false }));
  }

  function showInlineOptimizeWindow(originalPrompt, optimizedPrompt, titles) {
    const promptElement = document.querySelector(
      ['textarea[name="prompt"]', 'input[name="prompt"]', 'textarea[name="text"]', 'input[name="text"]'].join(',')
    );
    if (!promptElement) return false;

    let host = promptElement.closest('.promptPanel') || promptElement.closest('label') || promptElement.parentElement;
    if (!host) return false;

    let box = host.querySelector('.rag-optimize-inline');
    if (!box) {
      box = document.createElement('div');
      box.className = 'rag-optimize-inline';
      box.style.cssText = 'margin-top:8px;border:1px solid #d9e0ea;border-radius:8px;background:#f8fafc;padding:10px;color:#172033';
      host.appendChild(box);
    }

    box.innerHTML = '';
    const header = document.createElement('div');
    header.style.cssText = 'font-size:12px;color:#64748b;margin-bottom:6px';
    header.textContent = titles.length ? '检测到：' + titles.join('、') : '飞书知识库优化结果';
    const pre = document.createElement('pre');
    pre.style.cssText = 'white-space:pre-wrap;margin:0 0 8px;font-size:12px;line-height:1.6;max-height:180px;overflow:auto';
    pre.textContent = optimizedPrompt;
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap';
    const applyBtn = document.createElement('button');
    applyBtn.type = 'button';
    applyBtn.textContent = '应用优化结果';
    applyBtn.style.cssText = 'padding:6px 10px;border-radius:6px;border:1px solid #2563eb;background:#2563eb;color:#fff;cursor:pointer';
    applyBtn.addEventListener('click', () => {
      updatePromptElement(optimizedPrompt);
      box.remove();
      notify('已应用优化提示词，请再次点击生成。');
    });
    const keepBtn = document.createElement('button');
    keepBtn.type = 'button';
    keepBtn.textContent = '保持原提示词';
    keepBtn.style.cssText = 'padding:6px 10px;border-radius:6px;border:1px solid #d1d5db;background:#fff;color:#111;cursor:pointer';
    keepBtn.addEventListener('click', () => {
      box.remove();
      notify('已保持原提示词，请再次点击生成。');
    });
    actions.append(applyBtn, keepBtn);
    box.append(header, pre, actions);
    return true;
  }

  const originalFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    const options = init || {};
    if (!isGenerationRequest(url) || String(options.method || 'GET').toUpperCase() !== 'POST') {
      return originalFetch(input, init);
    }

    const prompt = readPrompt(options.body);
    if (!prompt || prompt.includes(MARKER) || prompt === lastReviewedPrompt) {
      if (prompt === lastReviewedPrompt) lastReviewedPrompt = null;
      return originalFetch(input, init);
    }

    try {
      const result = await preflight(prompt);
      if (!result || !result.detected) {
        return originalFetch(input, init);
      }

      const titles = (result.matches || []).map((item) => item.title).filter(Boolean);
      const shown = showInlineOptimizeWindow(prompt, result.updated_prompt || prompt, titles);
      if (!shown) notify('飞书知识库检测到相关规则，已生成优化提示词。请再次点击生成保持原提示词。');

      lastReviewedPrompt = prompt;
      return new Response(JSON.stringify({
        ok: false,
        error: '已暂停生成，请处理提示词优化结果后再次生成',
        rag_review: true
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      });
    } catch (error) {
      if (error && error.name === 'AbortError') throw error;
      return originalFetch(input, init);
    }
  };
})();
