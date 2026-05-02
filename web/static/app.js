// Custom model-name combobox: type-to-filter + tap-to-select.
// Replaces native <datalist> because iOS Safari / WeChat browser don't show
// it reliably on mobile.
(function () {
  const input = document.getElementById('model');
  const list = document.getElementById('model-list');
  if (!input || !list) return;
  const items = Array.from(list.querySelectorAll('.combo-item'));

  function filter(q) {
    const ql = (q || '').toLowerCase().trim();
    let visible = 0;
    items.forEach((it) => {
      const v = (it.getAttribute('data-value') || '').toLowerCase();
      const match = ql === '' || v.includes(ql);
      it.hidden = !match;
      if (match) visible++;
    });
    list.hidden = visible === 0;
  }

  input.addEventListener('focus', () => filter(input.value));
  input.addEventListener('input', () => filter(input.value));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') list.hidden = true;
  });

  items.forEach((it) => {
    // pointerdown beats focus loss; preventDefault keeps input focused so
    // mobile keyboard doesn't close before we set the value.
    it.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      input.value = it.getAttribute('data-value');
      list.hidden = true;
      input.blur(); // dismiss mobile keyboard after selection
    });
  });

  // close on outside tap (works on both mouse and touch)
  document.addEventListener('pointerdown', (e) => {
    if (e.target === input || list.contains(e.target)) return;
    list.hidden = true;
  });
})();


(function () {
  const form = document.getElementById('detect-form');
  if (!form) return;
  const submitBtn = document.getElementById('submit-btn');
  const errBox = document.getElementById('form-error');

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    errBox.hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = 'Submitting…';

    const fd = new FormData(form);
    try {
      const r = await fetch('/api/detect', {method: 'POST', body: fd});
      if (!r.ok) {
        const j = await r.json().catch(() => ({detail: 'request failed'}));
        throw new Error(j.detail || ('HTTP ' + r.status));
      }
      const j = await r.json();
      // wipe the api_key from the form so it can't be re-sent or peeked at
      form.api_key.value = '';
      location.href = '/r/' + j.job_id;
    } catch (e) {
      errBox.hidden = false;
      errBox.textContent = e.message || 'Submission failed';
      submitBtn.disabled = false;
      submitBtn.textContent = 'Start Detection';
    }
  });
})();
