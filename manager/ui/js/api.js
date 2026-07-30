/**
 * NanoClaw Manager — API 客户端
 * 封装所有与 Launcher 后端的通信。
 */

const API = {
  // ── Config ──────────────────────────────────────
  async getConfig() {
    return fetch('/api/config').then(r => r.json());
  },

  async updateConfig(data) {
    return fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }).then(r => r.json());
  },

  async updateApiKey(key) {
    return fetch('/api/config/apikey', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: key }),
    }).then(r => r.json());
  },

  // ── MCP ─────────────────────────────────────────
  async listMcpServers() {
    return fetch('/api/mcp/servers').then(r => r.json());
  },

  async addMcpServer(name, config) {
    return fetch('/api/mcp/servers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, config }),
    }).then(r => r.json());
  },

  async updateMcpServer(name, config) {
    return fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ config }),
    }).then(r => r.json());
  },

  async deleteMcpServer(name) {
    return fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }).then(r => r.json());
  },

  async toggleMcpServer(name) {
    return fetch(`/api/mcp/servers/${encodeURIComponent(name)}/toggle`, {
      method: 'POST',
    }).then(r => r.json());
  },

  // ── Gateway ─────────────────────────────────────
  async startGateway() {
    return fetch('/api/gateway/start', { method: 'POST' }).then(r => r.json());
  },

  async stopGateway() {
    return fetch('/api/gateway/stop', { method: 'POST' }).then(r => r.json());
  },

  async getGatewayStatus() {
    return fetch('/api/gateway/status').then(r => r.json());
  },
  async getGatewayWebUrl() {
    return fetch('/api/gateway/web-url').then(r => r.json());
  },

  // ── Utility ─────────────────────────────────────
  async openExplorer() {
    return fetch('/api/util/open-explorer', { method: 'POST' }).then(r => r.json());
  },

  async openCli() {
    return fetch('/api/util/open-cli', { method: 'POST' }).then(r => r.json());
  },

  async getWorkspacePath() {
    return fetch('/api/util/workspace-path').then(r => r.json());
  },
};

/** Toast 通知系统 */
const Toast = {
  show(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateX(20px)';
      el.style.transition = 'all 200ms ease';
      setTimeout(() => el.remove(), 200);
    }, 3000);
  },

  success(msg) { this.show(msg, 'success'); },
  error(msg) { this.show(msg, 'error'); },
  info(msg) { this.show(msg, 'info'); },
};
