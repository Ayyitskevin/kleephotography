// Each editor owns its text and receipt. No storage, sender or contact logging.
(function () {
  const form = document.querySelector('[data-draft-editor]');
  if (!form) return;
  const subject = form.elements.namedItem('subject');
  const body = form.elements.namedItem('body');
  const token = form.elements.namedItem('token');
  const feedback = form.querySelector('[data-draft-feedback]');
  const dirtyLabel = form.querySelector('[data-draft-dirty]');
  const count = form.querySelector('[data-draft-count]');
  const buttons = Array.from(form.querySelectorAll('[data-draft-copy]'));
  const snapshot = () => JSON.stringify([subject.value, body.value, token.value]);
  const original = snapshot();
  let submitting = false;
  let busy = false;
  const dirty = () => form.dataset.unsaved === 'true' || snapshot() !== original;
  const refresh = () => {
    dirtyLabel.textContent = dirty() ? 'Unsaved changes — save to keep this text.' : 'Saved draft';
    count.textContent = `${Array.from(body.value).length} characters`;
    feedback.textContent = '';
  };
  form.addEventListener('input', refresh);
  form.addEventListener('submit', () => { submitting = true; });
  window.addEventListener('beforeunload', (event) => {
    if (!submitting && dirty()) { event.preventDefault(); event.returnValue = ''; }
  });
  form.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-draft-copy]');
    if (!button || !form.contains(button) || button.disabled || busy) return;
    const part = button.dataset.draftCopy;
    if (!['subject', 'body'].includes(part) || !form.dataset.copyUrl) return;
    const captured = snapshot();
    const enabled = buttons.filter((b) => !b.disabled);
    enabled.forEach((b) => { b.disabled = true; });
    busy = true;
    feedback.textContent = 'Checking recipient and draft…';
    try {
      const response = await fetch(form.dataset.copyUrl, {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Could not verify this draft. Sign in and reopen it before copying.');
      }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not verify this draft. Reopen it before copying.');
      if (String(result.draft_id) !== form.dataset.draftId || String(result.prospect_id) !== form.dataset.prospectId) {
        throw new Error('The draft response belongs to another record. Reopen this draft.');
      }
      if (!form.isConnected || document.visibilityState === 'hidden') return;
      if (snapshot() !== captured) throw new Error('Text changed while checking. Review it and copy again.');
      // Copy the operator's exact current text, including unsaved edits; the
      // server checked that same form without saving it or recording contact.
      try { await navigator.clipboard.writeText(part === 'body' ? body.value : subject.value); }
      catch { throw new Error('Copy unavailable. Select and copy the reviewed text manually.'); }
      if (!form.isConnected) return;
      feedback.textContent = snapshot() === captured
        ? `${part === 'body' ? 'Message' : 'Subject'} copied. Nothing sent or logged.${dirty() ? ' Save to keep your edits.' : ''}`
        : 'The earlier text was copied. Review your changes and copy again.';
    } catch (error) {
      if (form.isConnected) feedback.textContent = error.message || 'Could not copy. Your text is unchanged.';
    } finally {
      busy = false;
      enabled.forEach((b) => { b.disabled = false; });
    }
  });
  refresh();
})();
