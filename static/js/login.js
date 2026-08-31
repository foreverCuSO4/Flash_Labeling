let mode = 'login';
const tabLogin = document.getElementById('tabLogin');
const tabRegister = document.getElementById('tabRegister');
const nameField = document.getElementById('nameField');
const submitBtn = document.getElementById('submitBtn');
const errMsg = document.getElementById('errMsg');

function setMode(m) {
  mode = m;
  nameField.classList.toggle('hidden', m === 'login');
  document.getElementById('name').required = (m === 'register');
  submitBtn.textContent = m === 'login' ? 'Sign In' : 'Register';
  tabLogin.style.opacity = m === 'login' ? '1' : '0.5';
  tabRegister.style.opacity = m === 'register' ? '1' : '0.5';
  hideErr(errMsg);
}
tabLogin.onclick = () => setMode('login');
tabRegister.onclick = () => setMode('register');

document.getElementById('authForm').onsubmit = async (e) => {
  e.preventDefault();
  hideErr(errMsg);
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  try {
    if (mode === 'register') {
      const name = document.getElementById('name').value.trim();
      await API.post('/api/auth/register', { email, name, password });
    } else {
      await API.post('/api/auth/login', { email, password });
    }
    window.location.href = '/projects.html';
  } catch (err) {
    showErr(errMsg, err.detail || 'Request failed');
  }
};

// Redirect if already logged in
API.get('/api/auth/me').then(() => { window.location.href = '/projects.html'; }).catch(() => {});
