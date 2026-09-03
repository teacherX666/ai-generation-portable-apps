(function () {
  if (window.__portalRagInterceptorInstalled) return;
  window.__portalRagInterceptorInstalled = true;

  const PREFLIGHT_URL = '/rag-assistant/api/rag/preflight';
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

    try {
      const result = await preflight(prompt);
      if (!result || !result.detected) {
        return originalFetch(input, init);
      }

      updatePromptElement(result.updated_prompt || prompt);
      const titles = (result.matches || []).map((item) => item.title).filter(Boolean).join('、');
      notify(`飞书知识库检测到相关规则：${titles || '已自动补充提示词'}。提示词已更新，请再次点击生成。`);

      const paused = new Error('Generation paused for RAG review');
      paused.name = 'AbortError';
      throw paused;
    } catch (error) {
      if (error && error.name === 'AbortError') throw error;
      return originalFetch(input, init);
    }
  };
})();
