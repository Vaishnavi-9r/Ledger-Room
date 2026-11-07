/* LedgerRoom website onboarding and account linking. Conversations stay in Mattermost. */
const mattermostCallbackStatus = new URLSearchParams(location.search).get('mattermost');
if (mattermostCallbackStatus) history.replaceState({}, '', location.pathname);

function mattermostWorkspaceStatus(status, errorCode, {isOwner = false, linked = false} = {}) {
  const descriptions = {
    ready: 'The private Mattermost channel is ready for linked business members and the agent.',
    partial: 'The private channel exists, but one or more linked participants still need setup.',
    pending_identity_link: 'Link your Mattermost account before a private channel can be provisioned.',
    pending_configuration: 'Mattermost integration settings are incomplete.',
    pending: 'The private Mattermost channel has not been provisioned yet.',
    error: 'Mattermost could not finish channel provisioning. Check the integration configuration and retry.',
  };
  if (!status) {
    if (!isOwner) return 'The business owner will provision the private finance channel after accepting membership.';
    return linked
      ? 'Ready to create a new private LedgerRoom finance channel. The public Switch channel will not be reused.'
      : 'Link your Mattermost account above to enable private channel provisioning.';
  }
  return descriptions[status] || (errorCode ? 'Mattermost setup needs attention.' : 'Mattermost workspace status updated.');
}

function renderMattermostWorkspace() {
  const business = selectedBusiness();
  if (!business) return workspacePicker();
  const membership = state.user.memberships.find(item => item.business_id === business.id);
  const isOwner = membership?.role === 'owner';
  const linked = state.mattermostIdentity;
  const room = state.mattermostRoomFor === business.id ? state.mattermostRoom : null;
  const roomError = state.mattermostRoomFor === business.id ? state.mattermostRoomError : null;
  const canProvision = isOwner && Boolean(linked);
  const provisionLabel = room?.mattermost_channel_id
    ? 'Sync private channel members'
    : linked ? 'Create private finance channel' : 'Link Mattermost to enable provisioning';
  app.innerHTML = `<div class="app-shell"><aside class="sidebar"><div class="brand"><span class="brand-mark">L</span><span class="brand-name">LedgerRoom</span></div><button class="workspace-switch" id="switch-workspace"><span class="workspace-avatar">${esc(initials(business.business_name))}</span><span class="workspace-meta"><b>${esc(business.business_name)}</b><span>${esc(membership?.role || 'member')} workspace</span></span><span class="nav-icon">⌄</span></button><div class="side-label">Workspace</div><button class="nav-item active" id="workspace-home"><span class="nav-icon">◉</span><span class="nav-text">Mattermost setup</span></button><button class="nav-item" id="team-link"><span class="nav-icon">♧</span><span class="nav-text">People & invitations</span></button><div class="spacer"></div>${userFooter()}</aside><main class="main"><header class="topbar"><div class="crumb">${esc(business.business_name)} <span> / </span> <b>Mattermost setup</b></div><div class="top-actions"><button class="button secondary" id="switch-top">Switch workspace</button></div></header><section class="content"><div class="page-head"><div><div class="auth-eyebrow">LEDGERROOM BUSINESS WORKSPACE</div><h1>Continue in Mattermost</h1><p>LedgerRoom keeps account access and permissions. Your team conversation lives in Mattermost.</p></div><span class="role-tag">${esc(membership?.role || 'member')}</span></div><div class="workspace-card"><div class="workspace-avatar">${esc(initials(business.business_name))}</div><div><h3>${esc(business.business_name)}</h3><p>${esc(business.city)}, ${esc(business.state)} · ${esc(business.business_type)}</p></div></div><section class="room" aria-labelledby="mattermost-account-heading"><div class="room-head"><div><div class="room-title" id="mattermost-account-heading">Mattermost account</div><div class="room-sub">Link your own authenticated Mattermost identity</div></div><span class="role-tag">${linked ? 'Linked' : 'Not linked'}</span></div><div class="room-body"><div class="empty-state"><div class="empty-icon">↗</div><h2>${linked ? 'Your account is linked' : 'Connect your Mattermost account'}</h2><p>${linked ? `Verified Mattermost identity on ${esc(linked.server_url)}.` : 'LedgerRoom will verify your identity through Mattermost OAuth before adding you to a business channel.'}</p><div class="form-error" id="mattermost-error" role="alert" aria-live="polite">${esc(state.mattermostIdentityError || '')}</div>${linked ? `<a class="button secondary" href="${esc(linked.server_url)}" target="_blank" rel="noopener noreferrer">Open Mattermost</a>` : ''}<button class="button primary" id="link-mattermost" ${linked ? 'disabled' : ''}>${linked ? 'Mattermost account linked' : 'Link Mattermost account'}</button><button class="button secondary" id="refresh-mattermost">Check link status</button></div></div></section><section class="room" aria-labelledby="mattermost-room-heading"><div class="room-head"><div><div class="room-title" id="mattermost-room-heading">Private finance channel</div><div class="room-sub">${isOwner ? 'Owner-only provisioning for this business. A new private channel will be created; the public Switch channel will not be reused.' : 'Only the business owner can provision this channel. Your access follows active LedgerRoom membership and verified Mattermost identity.'}</div></div><span class="role-tag">${room ? esc(room.mattermost_status) : 'Not provisioned'}</span></div><div class="room-body"><div class="empty-state"><div class="empty-icon">▣</div><h2>${room?.mattermost_channel_name ? `#${esc(room.mattermost_channel_name)}` : 'Private LedgerRoom channel'}</h2><p id="mattermost-room-copy">${esc(mattermostWorkspaceStatus(room?.mattermost_status, room?.mattermost_error_code, {isOwner, linked: Boolean(linked)}))}</p>${isOwner ? `<button class="button primary" id="provision-mattermost" ${canProvision ? '' : 'disabled'}>${provisionLabel}</button>` : ''}<div class="form-error" id="room-error" role="alert" aria-live="polite">${esc(roomError || '')}</div></div></div></section><div class="notice">${isOwner ? 'After provisioning, invite the accountant from People & invitations. They accept using their own LedgerRoom account, link their own Mattermost identity, and are synchronized into this private channel.' : 'Your Mattermost channel access follows the accepted LedgerRoom invitation and your own verified identity link.'}</div></section></main></div>`;

  const selectWorkspace = () => { state.selected = null; localStorage.removeItem('lr_business'); render(); };
  $('#switch-workspace').onclick = selectWorkspace;
  $('#switch-top').onclick = selectWorkspace;
  $('#workspace-home').onclick = () => renderMattermostWorkspace();
  $('#team-link').onclick = () => showMattermostMembers(business);
  $('#refresh-mattermost').onclick = () => refreshMattermostIdentity(true);
  $('#link-mattermost').onclick = async event => {
    const button = event.currentTarget;
    const error = $('#mattermost-error');
    error.textContent = '';
    button.disabled = true;
    button.textContent = 'Connecting…';
    try {
      const result = await request('/api/v1/integrations/mattermost/link', {method:'POST'});
      location.assign(result.authorization_url);
    } catch (err) {
      error.textContent = err.message;
      button.disabled = false;
      button.textContent = 'Link Mattermost account';
    }
  };
  if ($('#provision-mattermost')) $('#provision-mattermost').onclick = async event => {
    const button = event.currentTarget;
    const error = $('#room-error');
    const copy = $('#mattermost-room-copy');
    error.textContent = '';
    button.disabled = true;
      button.textContent = room?.mattermost_channel_id ? 'Syncing private channel…' : 'Creating private channel…';
    try {
      state.mattermostRoom = await request(`/api/v1/businesses/${encodeURIComponent(business.id)}/room`, {method:'POST'});
      state.mattermostRoomFor = business.id;
      state.mattermostRoomError = null;
      renderMattermostWorkspace();
    } catch (err) {
      error.textContent = err.message;
      button.disabled = false;
      button.textContent = room?.mattermost_channel_id ? 'Retry private channel sync' : 'Retry private channel creation';
      copy.textContent = 'Provisioning failed. You can retry after checking setup.';
    }
  };
  bindLogout();
  if (mattermostCallbackStatus === 'linked') toast('Mattermost identity linked.');
}

workspace = renderMattermostWorkspace;

async function refreshMattermostIdentity(showStatus = false) {
  await loadMattermostWorkspaceData();
  if (state.user && selectedBusiness()) renderMattermostWorkspace();
  if (showStatus) toast(state.mattermostIdentity ? 'Mattermost account is linked.' : 'No Mattermost account is linked yet.', !state.mattermostIdentity);
}

async function showMattermostMembers(business) {
  try {
    const [people, invites] = await Promise.all([
      request(`/api/v1/businesses/${encodeURIComponent(business.id)}/members`),
      request(`/api/v1/businesses/${encodeURIComponent(business.id)}/invitations`).catch(() => ({invitations:[]})),
    ]);
    const role = state.user.memberships.find(item => item.business_id === business.id)?.role;
    document.body.insertAdjacentHTML('beforeend', `<div class="modal-backdrop"><div class="modal"><h2>People & invitations</h2><p>Each person uses a separate LedgerRoom account and links their own Mattermost identity.</p><div class="workspace-list">${people.members.map(member => `<div class="workspace-choice" style="cursor:default"><div class="user-avatar">${esc(initials(member.full_name))}</div><div class="workspace-meta"><b>${esc(member.full_name)}</b><span>${esc(member.email)}</span></div><span class="role-tag">${esc(member.role)}</span></div>`).join('') || '<p>No active members.</p>'}</div>${role === 'owner' ? `<form id="mattermost-invite-form" style="margin-top:20px">${field('Invite accountant by email','invite_email','email',true,'accountant@firm.com')}<button class="button primary" type="submit">Create invitation</button></form><div id="invite-result" role="status"></div>` : ''}<div class="pending-invites">${invites.invitations.filter(invite => invite.status === 'pending').map(invite => `<div class="workspace-choice" style="cursor:default"><div class="workspace-meta"><b>${esc(invite.email)}</b><span>Invitation · expires ${new Date(invite.expires_at).toLocaleDateString()}</span></div>${role === 'owner' ? `<button class="button secondary revoke-invite" data-id="${esc(invite.id)}">Revoke</button>` : ''}</div>`).join('')}</div><div class="modal-actions"><button type="button" class="button secondary" id="close-people">Done</button></div></div></div>`);
    $('.modal-backdrop').onclick = event => { if (event.target.classList.contains('modal-backdrop')) event.currentTarget.remove(); };
    $('#close-people').onclick = () => $('.modal-backdrop').remove();
    $$('.revoke-invite').forEach(button => button.onclick = async () => {
      try {
        await request(`/api/v1/businesses/${encodeURIComponent(business.id)}/invitations/${encodeURIComponent(button.dataset.id)}/revoke`, {method:'POST'});
        button.closest('.workspace-choice').remove();
        toast('Invitation revoked.');
      } catch (err) { toast(err.message, true); }
    });
    const form = $('#mattermost-invite-form');
    if (form) form.onsubmit = async event => {
      event.preventDefault();
      const button = form.querySelector('button[type=submit]');
      const resultHost = $('#invite-result');
      button.disabled = true;
      button.textContent = 'Creating invitation…';
      resultHost.textContent = '';
      try {
        const response = await request(`/api/v1/businesses/${encodeURIComponent(business.id)}/invitations`, {method:'POST',body:JSON.stringify({email:new FormData(form).get('invite_email')})});
        const link = response.shareable_invitation_link || response.development_invitation_link;
        if (link) {
          const safeLink = esc(link);
          const message = `Join ${business.business_name} on LedgerRoom: ${link}`;
          const whatsappUrl = `https://wa.me/?text=${encodeURIComponent(message)}`;
          resultHost.innerHTML = `<div class="dev-invite-link"><b>Invitation for ${esc(response.invitation.email)}</b><p>This link expires and can be accepted only by the invited email's authenticated LedgerRoom account.</p><button type="button" class="button secondary" id="copy-invite-link" data-url="${safeLink}">Copy invitation link</button> <a class="button secondary" href="${esc(whatsappUrl)}" target="_blank" rel="noopener noreferrer">Share with WhatsApp</a></div>`;
          $('#copy-invite-link').onclick = async copyButton => {
            try { await navigator.clipboard.writeText(copyButton.currentTarget.dataset.url); toast('Invitation link copied.'); }
            catch { toast('Clipboard access is unavailable. Open WhatsApp to share the link.', true); }
          };
        } else {
          resultHost.textContent = 'Invitation submitted to the configured email provider.';
        }
        button.textContent = 'Invitation created';
      } catch (err) {
        resultHost.textContent = err.message;
        button.disabled = false;
        button.textContent = 'Create invitation';
      }
    };
  } catch (err) { toast(err.message, true); }
}

async function loadMattermostWorkspaceData() {
  if (!state.user || !selectedBusiness()) return;
  const business = selectedBusiness();
  state.mattermostRoomFor = business.id;
  const [identityResult, roomResult] = await Promise.allSettled([
    request('/api/v1/integrations/mattermost/me'),
    request(`/api/v1/businesses/${encodeURIComponent(business.id)}/room`),
  ]);
  if (identityResult.status === 'fulfilled') {
    state.mattermostIdentity = identityResult.value;
    state.mattermostIdentityError = null;
  } else {
    state.mattermostIdentity = null;
    state.mattermostIdentityError = identityResult.reason?.status === 404 ? null : identityResult.reason?.message || 'Mattermost identity status is unavailable.';
  }
  if (roomResult.status === 'fulfilled') {
    state.mattermostRoom = roomResult.value;
    state.mattermostRoomError = null;
  } else if (roomResult.reason?.status === 404) {
    state.mattermostRoom = null;
    state.mattermostRoomError = null;
  } else {
    state.mattermostRoom = null;
    state.mattermostRoomError = roomResult.reason?.message || 'Private channel status is unavailable.';
  }
}

async function hydrateMattermostWorkspace() {
  await loadMattermostWorkspaceData();
  if (state.user && selectedBusiness()) renderMattermostWorkspace();
}

const mattermostOriginalLoadWorkspace = loadWorkspace;
loadWorkspace = async function() {
  await mattermostOriginalLoadWorkspace();
  if (state.user && selectedBusiness()) await hydrateMattermostWorkspace();
};
