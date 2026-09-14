async function init() {
  let me;
  try { me = await API.get('/api/auth/me'); } catch { window.location.href = appPath('/'); return; }
  document.getElementById('userName').textContent = me.name;
  document.getElementById('userAvatar').src = appPath(`/api/users/${me.id}/avatar`);

  document.getElementById('logoutBtn').onclick = async () => {
    await API.post('/api/auth/logout');
    window.location.href = appPath('/');
  };

  loadUsers();
}

async function loadUsers() {
  const list = document.getElementById('userList');
  const emptyMsg = document.getElementById('emptyMsg');
  try {
    const users = await API.get('/api/users');
    window.__usersData = users;
    emptyMsg.classList.toggle('hidden', users.length > 0);
    renderUsers(users);
  } catch { list.innerHTML = `<p class="error">${t('common.loadUsersFailed')}</p>`; }
}

function renderUsers(users = window.__usersData || []) {
  const list = document.getElementById('userList');
  list.innerHTML = users.map(u => `
    <div class="panel row">
      <img class="avatar avatar-lg" src="${appPath(u.avatar_url)}" alt="${esc(u.name)}">
      <div>
        <h3 style="font-family:var(--font-display);font-size:20px;letter-spacing:0.96px;">${esc(u.name)}</h3>
        <p class="text-mute" style="font-size:14px;">${esc(u.email)}</p>
        <p class="text-mute" style="font-size:13px;">${t('users.joined')} ${esc(u.created_at.slice(0, 10))}</p>
      </div>
    </div>
  `).join('');
}

window.addEventListener('languagechange', () => {
  if (window.__usersData) renderUsers(window.__usersData);
});

init();
