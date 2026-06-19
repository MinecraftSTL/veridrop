// Mobile nav hamburger toggle. Pure aria-expanded toggling; CSS does the
// rest via the sibling selector. Closes on outside tap, on ESC, and on
// link tap so the dropdown doesn't linger after navigation.
(function () {
  const toggle = document.querySelector('.nav-toggle');
  const nav = document.getElementById('site-nav');
  if (!toggle || !nav) return;

  function setOpen(open) {
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    toggle.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
  }

  toggle.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = toggle.getAttribute('aria-expanded') === 'true';
    setOpen(!isOpen);
  });

  // Close on tap outside the dropdown.
  document.addEventListener('click', (e) => {
    if (toggle.getAttribute('aria-expanded') !== 'true') return;
    if (nav.contains(e.target) || toggle.contains(e.target)) return;
    setOpen(false);
  });

  // Close after a nav link is tapped — otherwise the panel stays open over
  // the new page transition (jarring on mobile).
  nav.addEventListener('click', (e) => {
    if (e.target.tagName === 'A') setOpen(false);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setOpen(false);
  });
})();


// Custom model-name combobox: type-to-filter + tap-to-select.
// Replaces native <datalist> because iOS Safari / WeChat browser don't show
// it reliably on mobile.
(function () {
  const input = document.getElementById('model');
  const list = document.getElementById('model-list');
  if (!input || !list) return;
  let items = Array.from(list.querySelectorAll('.combo-item'));

  // De-emphasize non-matches instead of hiding them. Users repeatedly
  // expected the dropdown to show ALL probed models even after typing a
  // partial name (so they can compare options or pick a sibling). Hiding
  // made the relay's full whitelist invisible — exactly the opposite of
  // what /api/probe was meant to surface. Dimming preserves discoverability
  // while still highlighting the current text query.
  function filter(q) {
    const ql = (q || '').toLowerCase().trim();
    items.forEach((it) => {
      const v = (it.getAttribute('data-value') || '').toLowerCase();
      const match = ql === '' || v.includes(ql);
      it.classList.toggle('no-match', !match);
      it.hidden = false;
    });
    list.hidden = items.length === 0;
  }

  function bindItem(it) {
    // pointerdown beats focus loss; preventDefault keeps input focused so
    // mobile keyboard doesn't close before we set the value.
    it.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      input.value = it.getAttribute('data-value');
      list.hidden = true;
      input.blur();
    });
  }
  items.forEach(bindItem);

  // Exposed so the probe layer can replace the suggestions with whatever
  // the relay actually advertises. Falls back to the static template list
  // if probe fails / relay doesn't expose /v1/models.
  window.veridropSetModelChoices = function (values) {
    list.innerHTML = '';
    values.forEach((v) => {
      const li = document.createElement('li');
      li.className = 'combo-item';
      li.setAttribute('data-value', v);
      li.textContent = v;
      list.appendChild(li);
    });
    items = Array.from(list.querySelectorAll('.combo-item'));
    items.forEach(bindItem);
  };

  input.addEventListener('focus', () => filter(input.value));
  input.addEventListener('input', () => filter(input.value));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') list.hidden = true;
  });

  document.addEventListener('pointerdown', (e) => {
    if (e.target === input || list.contains(e.target)) return;
    list.hidden = true;
  });
})();


// Pre-submission probe: hit /api/probe on api_key blur, render an inline
// pill below the api_key input describing what the relay carries, replace
// the model dropdown with the actually-available models, and (when the
// current protocol has 0 matches) offer one-click handoff to a protocol
// the relay DOES carry.
(function () {
  const protocol =
    location.pathname.startsWith('/claude') ? 'anthropic' :
    location.pathname.startsWith('/openai') ? 'openai' :
    location.pathname.startsWith('/gemini') ? 'gemini' : null;
  if (!protocol) return;

  const protoLabel = {anthropic: 'Claude', openai: 'OpenAI', gemini: 'Gemini'}[protocol];
  const protoPath = {anthropic: '/claude', openai: '/openai', gemini: '/gemini'};

  const baseUrlInput = document.getElementById('base_url');
  const apiKeyInput = document.getElementById('api_key');
  const modelInput = document.getElementById('model');
  if (!baseUrlInput || !apiKeyInput) return;

  // Inject pill container right after the api_key field's hint.
  const apiKeyField = apiKeyInput.closest('.field');
  const pill = document.createElement('div');
  pill.id = 'probe-pill';
  pill.className = 'probe-pill';
  pill.hidden = true;
  apiKeyField.appendChild(pill);

  let inflight = null;
  let lastKey = null;

  async function runProbe() {
    const baseUrl = baseUrlInput.value.trim();
    const apiKey = apiKeyInput.value.trim();
    if (!baseUrl || !apiKey || apiKey.length < 8) return;
    if (!/^https?:\/\//.test(baseUrl)) return;

    const key = baseUrl + '|' + apiKey.length + ':' + apiKey.slice(-4);
    if (key === lastKey) return; // already probed this combo
    lastKey = key;

    setPill('neutral', '正在识别中转站可用模型...');
    if (inflight) inflight.abort && inflight.abort();
    const ctrl = new AbortController();
    inflight = ctrl;

    const fd = new FormData();
    fd.set('base_url', baseUrl);
    fd.set('api_key', apiKey);
    let r, data;
    try {
      r = await fetch('/api/probe', {method: 'POST', body: fd, signal: ctrl.signal});
      data = await r.json();
    } catch (e) {
      if (e.name === 'AbortError') return;
      setPill('warn', '探测失败,但不影响检测继续 — 你填的模型会被直接尝试');
      return;
    }
    if (r.status === 429) {
      // Rate limited — surface clearly and keep submit enabled so the user
      // can still proceed (they're not blocked from detection itself).
      setPill('warn', (data.error || '探测过于频繁,稍后再试') + '(检测仍可正常提交)');
      lastKey = null; // allow retry after backoff
      return;
    }
    renderProbeResult(data);
  }

  function renderProbeResult(data) {
    if (!data.ok) {
      // Auth fail vs other errors — auth_ok=false is the only blocking case
      if (data.auth_ok === false) {
        setPill('fail', (data.error || '鉴权失败'));
      } else {
        setPill('warn', (data.error || '探测失败') + ' — 不影响检测继续');
      }
      return;
    }

    if (!data.models_endpoint_supported) {
      setPill('neutral', (data.note || '该中转站不暴露 /v1/models') + '(检测可正常进行)');
      return;
    }

    const myModels = (data.by_protocol && data.by_protocol[protocol]) || [];
    const total = data.raw_count || 0;

    if (myModels.length === 0) {
      // The headline case: cross-protocol suggestion.
      const others = Object.keys(data.by_protocol || {})
        .filter((p) => p !== protocol && data.by_protocol[p].length > 0)
        .map((p) => ({proto: p, count: data.by_protocol[p].length, sample: data.by_protocol[p][0]}));

      let html =
        '<div class="probe-headline">该中转站没有任何 ' + escapeHtml(protoLabel) + ' 模型</div>' +
        '<div class="probe-detail">已识别 ' + total + ' 个模型,但都不属于本检测协议。</div>';
      if (others.length) {
        html += '<div class="probe-actions">';
        others.forEach((o) => {
          const label = {anthropic: 'Claude', openai: 'OpenAI', gemini: 'Gemini'}[o.proto];
          html +=
            '<button type="button" class="btn btn-ghost probe-action" data-handoff="' + o.proto + '">' +
            '改用 ' + label + ' 协议 (' + o.count + ' 个可用)</button>';
        });
        html += '</div>';
      }
      setPillHtml('warn', html);
      bindHandoff();
      // Disable submit — running detection here will produce 0% report.
      setSubmitEnabled(false, '该中转站没有 ' + protoLabel + ' 模型');
      return;
    }

    // Happy path: at least one model matches our protocol.
    const sample = myModels.slice(0, 4).join(', ');
    const more = myModels.length > 4 ? ` 等共 ${myModels.length} 个` : '';
    setPillHtml(
      'ok',
      '<div class="probe-headline">已识别 ' + total + ' 个模型,其中 ' + myModels.length + ' 个可用于本检测</div>' +
      '<div class="probe-detail">' + escapeHtml(sample) + escapeHtml(more) + '</div>'
    );

    // Replace the dropdown with what the relay actually carries.
    if (window.veridropSetModelChoices) {
      window.veridropSetModelChoices(myModels);
    }
    setSubmitEnabled(true);

    // Stash best_by_protocol globally — the submit handler reads it when
    // preflight 422s so it can offer a one-click swap to the recommended
    // model.
    window.veridropBestByProtocol = data.best_by_protocol || {};

    // If the user-typed model isn't in the list, auto-correct to the
    // protocol-preferred default rather than whatever sorts first
    // alphabetically. The backend computes "best" via each protocol's
    // pick_default_model — for OpenAI that's gpt-4o-mini, for Gemini it's
    // gemini-2.5-flash, etc. — so a /gemini → /openai handoff lands on a
    // sensible model instead of e.g. gpt-3.5-turbo or some preview SKU.
    const best = (data.best_by_protocol && data.best_by_protocol[protocol]) || myModels[0];
    if (modelInput && modelInput.value.trim() && !myModels.includes(modelInput.value.trim())) {
      modelInput.value = best;
    }
  }

  function setPill(level, text) {
    pill.className = 'probe-pill probe-' + level;
    pill.textContent = text;
    pill.hidden = false;
  }
  function setPillHtml(level, html) {
    pill.className = 'probe-pill probe-' + level;
    pill.innerHTML = html;
    pill.hidden = false;
  }

  function setSubmitEnabled(ok, reason) {
    const btn = document.getElementById('submit-btn');
    if (!btn) return;
    btn.disabled = !ok;
    btn.title = ok ? '' : (reason || '');
  }

  function bindHandoff() {
    pill.querySelectorAll('[data-handoff]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const target = btn.getAttribute('data-handoff');
        try {
          sessionStorage.setItem('veridrop:handoff', JSON.stringify({
            base_url: baseUrlInput.value.trim(),
            api_key: apiKeyInput.value.trim(),
            from: protocol,
          }));
        } catch (_) { /* sessionStorage unavailable — page navigates anyway */ }
        location.href = protoPath[target];
      });
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Trigger probe on api_key blur. Also re-probe when base_url changes
  // (after blur) so users editing both fields don't miss a re-check.
  apiKeyInput.addEventListener('blur', runProbe);
  baseUrlInput.addEventListener('blur', () => {
    lastKey = null; // base changed → invalidate dedup
    runProbe();
  });

  // Cross-protocol handoff: if we landed here from another protocol page,
  // pre-fill the form and immediately probe. Single-shot — clear after read
  // so a refresh doesn't reuse the key.
  try {
    const raw = sessionStorage.getItem('veridrop:handoff');
    if (raw) {
      sessionStorage.removeItem('veridrop:handoff');
      const data = JSON.parse(raw);
      if (data && data.base_url && data.api_key) {
        baseUrlInput.value = data.base_url;
        apiKeyInput.value = data.api_key;
        const fromLabel = {anthropic: 'Claude', openai: 'OpenAI', gemini: 'Gemini'}[data.from] || data.from;
        setPill('neutral', '已从 ' + fromLabel + ' 页面带入凭据,正在重新探测...');
        // Defer so the page paints first
        setTimeout(runProbe, 50);
      }
    }
  } catch (_) { /* malformed handoff — ignore */ }
})();


(function () {
  const form = document.getElementById('detect-form');
  if (!form) return;
  const submitBtn = document.getElementById('submit-btn');
  const errBox = document.getElementById('form-error');

  function endpointFor() {
    return form.getAttribute('data-endpoint')
      || (location.pathname.startsWith('/claude')
        ? '/api/detect/claude'
        : location.pathname.startsWith('/openai')
        ? '/api/detect/openai'
        : location.pathname.startsWith('/gemini')
        ? '/api/detect/gemini'
        : '/api/detect');
  }

  function currentProtocol() {
    return location.pathname.startsWith('/claude') ? 'anthropic' :
           location.pathname.startsWith('/openai') ? 'openai' :
           location.pathname.startsWith('/gemini') ? 'gemini' : null;
  }

  function renderModelDeadError(detail) {
    // Backend returns: {code, message, model, protocol, upstream_error}
    const proto = currentProtocol();
    const recommended = (window.veridropBestByProtocol || {})[proto];
    const dead = detail.model || '该模型';
    const reason = detail.upstream_error || '上游拒绝';

    errBox.innerHTML = '';
    errBox.hidden = false;
    errBox.classList.add('form-error-rich');

    const title = document.createElement('div');
    title.className = 'form-error-title';
    title.textContent = '该模型在中转站实际不可用';
    errBox.appendChild(title);

    const body = document.createElement('div');
    body.className = 'form-error-body';
    body.textContent = `${dead}: ${reason}`;
    errBox.appendChild(body);

    const actions = document.createElement('div');
    actions.className = 'form-error-actions';

    if (recommended && recommended !== dead) {
      const swapBtn = document.createElement('button');
      swapBtn.type = 'button';
      swapBtn.className = 'btn btn-primary';
      swapBtn.textContent = `换成 ${recommended} 重试`;
      swapBtn.addEventListener('click', () => {
        const modelInput = document.getElementById('model');
        if (modelInput) modelInput.value = recommended;
        errBox.hidden = true;
        errBox.classList.remove('form-error-rich');
        // Clear force flag if it was set by previous click.
        const force = form.querySelector('input[name="force"]');
        if (force) force.value = '';
        form.requestSubmit();
      });
      actions.appendChild(swapBtn);
    }

    const forceBtn = document.createElement('button');
    forceBtn.type = 'button';
    forceBtn.className = 'btn btn-ghost';
    forceBtn.textContent = '我知道,强制提交';
    forceBtn.title = 'preflight 偶尔会误判(例如 max_tokens 太小被代理拒)。强制提交后,如果模型真挂了,检测会以错误结果呈现。';
    forceBtn.addEventListener('click', () => {
      // Append a hidden force=1 field; the detect routes skip preflight when
      // it's set.
      let force = form.querySelector('input[name="force"]');
      if (!force) {
        force = document.createElement('input');
        force.type = 'hidden';
        force.name = 'force';
        form.appendChild(force);
      }
      force.value = '1';
      errBox.hidden = true;
      errBox.classList.remove('form-error-rich');
      form.requestSubmit();
    });
    actions.appendChild(forceBtn);

    errBox.appendChild(actions);
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    errBox.hidden = true;
    errBox.classList.remove('form-error-rich');
    submitBtn.disabled = true;
    submitBtn.textContent = '正在确认模型可用…';

    const fd = new FormData(form);
    try {
      const r = await fetch(endpointFor(), {method: 'POST', body: fd});
      if (r.status === 422) {
        const j = await r.json().catch(() => ({}));
        const detail = j && j.detail;
        if (detail && detail.code === 'model_not_alive') {
          renderModelDeadError(detail);
          submitBtn.disabled = false;
          submitBtn.textContent = '开始检测';
          return;
        }
      }
      if (!r.ok) {
        const j = await r.json().catch(() => ({detail: 'request failed'}));
        const msg = typeof j.detail === 'string' ? j.detail
          : (j.detail && j.detail.message) || ('HTTP ' + r.status);
        throw new Error(msg);
      }
      const j = await r.json();
      form.api_key.value = '';
      // Clear force flag so a subsequent submission goes through preflight.
      const force = form.querySelector('input[name="force"]');
      if (force) force.value = '';
      location.href = '/r/' + j.job_id;
    } catch (e) {
      errBox.hidden = false;
      errBox.textContent = e.message || 'Submission failed';
      submitBtn.disabled = false;
      submitBtn.textContent = '开始检测';
    }
  });
})();

// FAQ dual-mode toggle (通俗 / 开发者).
// Two <p data-mode="layperson|developer"> per question are both in DOM
// (so search engines index both); CSS hides whichever doesn't match the
// section's data-mode. Choice persists in localStorage so the user
// doesn't have to re-toggle every visit.
(() => {
  const STORAGE_KEY = 'veridrop_faq_mode';
  const sections = document.querySelectorAll('.faq[data-mode]');
  if (!sections.length) return;

  // Restore saved preference (if any) before any clicks.
  const saved = (() => {
    try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
  })();
  if (saved === 'layperson' || saved === 'developer') {
    sections.forEach((sec) => {
      sec.dataset.mode = saved;
      sec.querySelectorAll('.faq-mode-btn').forEach((b) => {
        const active = b.dataset.mode === saved;
        b.classList.toggle('faq-mode-active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      });
    });
  }

  // Click handler: switch mode + persist.
  sections.forEach((sec) => {
    sec.querySelectorAll('.faq-mode-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.mode;
        if (!mode) return;
        sec.dataset.mode = mode;
        sec.querySelectorAll('.faq-mode-btn').forEach((b) => {
          const active = b === btn;
          b.classList.toggle('faq-mode-active', active);
          b.setAttribute('aria-selected', active ? 'true' : 'false');
        });
        try { localStorage.setItem(STORAGE_KEY, mode); } catch { /* ignore */ }
      });
    });
  });
})();
