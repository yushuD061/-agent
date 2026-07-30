/**
 * NanoClaw Manager — 主应用
 * 路由 + 侧边栏导航 + 主题切换 + 初始化
 */

// ── 主题管理 ─────────────────────────────────
const THEME_KEY = 'nanoclaw_theme';

function getThemeSetting() {
  return localStorage.getItem(THEME_KEY) || 'dark';
}

function getEffectiveTheme() {
  const setting = getThemeSetting();
  if (setting === 'system') {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  return setting;
}

function applyTheme() {
  const theme = getEffectiveTheme();
  document.documentElement.setAttribute('data-theme', theme);
  // 更新按钮图标
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    const setting = getThemeSetting();
    btn.textContent = setting === 'light' ? '☀️' : setting === 'system' ? '🌓' : '🌙';
    btn.title = setting === 'light' ? '切换到深色' : setting === 'system' ? '切换到浅色' : '跟随系统';
  }
}

function cycleTheme() {
  const current = getThemeSetting();
  const next = current === 'dark' ? 'light' : current === 'light' ? 'system' : 'dark';
  localStorage.setItem(THEME_KEY, next);
  applyTheme();
}

// 监听系统主题变化
window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
  if (getThemeSetting() === 'system') applyTheme();
});

// ── 路由表 ─────────────────────────────────
const ROUTES = {
  settings: renderSettings,
  mcp: renderMcp,
  gateway: renderGateway,
};

// ── 页面切换 ───────────────────────────────
async function navigate(route) {
  document.querySelectorAll('.nav-item').forEach(item => {
    item.classList.toggle('active', item.dataset.route === route);
  });
  const renderFn = ROUTES[route];
  if (renderFn) {
    try {
      await renderFn();
    } catch (e) {
      console.error('Render error:', e);
      const container = document.getElementById('main-content');
      container.innerHTML = `
        <div class="page-body">
          <div class="empty-state">
            <div class="empty-state-icon">⚠️</div>
            <div class="empty-state-text">页面加载失败</div>
            <div style="margin-top:8px;font-size:12px;color:var(--color-fg-muted)">${e.message}</div>
          </div>
        </div>
      `;
    }
  }
}

// ── 侧边栏点击事件 ─────────────────────────
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    const route = item.dataset.route;
    if (route) navigate(route);
  });
});

// ── 主题按钮 ───────────────────────────────
document.getElementById('theme-toggle')?.addEventListener('click', cycleTheme);

// ── 启动 ───────────────────────────────────
applyTheme();
navigate('settings');
