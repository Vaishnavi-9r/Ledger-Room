document.addEventListener('submit', event => {
  if (event.target?.id !== 'chat-form') return;
  const status = document.querySelector('#chat-status');
  if (!status) return;
  status.textContent = 'LedgerRoom AI is working…';
  status.classList.add('processing-indicator');
  setTimeout(() => {
    if (!document.querySelector('#chat-form')) return;
    status.classList.remove('processing-indicator');
    if (status.textContent === 'LedgerRoom AI is working…') status.textContent = 'Shared · live updates';
  }, 15000);
}, true);
