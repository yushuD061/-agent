/**
 * 网关控制面板
 * Gateway 启动/停止/重启 + 实时日志 + 打开 Web/CLI
 */

let _logWs = null;
let _statusPollTimer = null;
let _gatewayWebUrl = 'http://127.0.0.1:8765';

async function renderGateway() {
  const container = document.getElementById('main-content');
  container.innerHTML = `
    <div class="page-header">
      <h1>🖥️ 网关</h1>
      <p>管理 NanoClaw Gateway 的运行状态</p>
    </div>
    <div class="page-body">
      <!-- 状态栏 -->
      <div class="gateway-status-bar" id="gateway-status-bar">
        <div class="gateway-status-dot stopped" id="gateway-dot"></div>
        <div>
          <div class="gateway-status-text" id="gateway-status-text">正在检测状态...</div>
          <div class="gateway-status-meta" id="gateway-status-meta"></div>
        </div>
        <div class="gateway-actions">
          <button class="btn btn-primary" id="btn-gw-start">▶ 启动</button>
          <button class="btn btn-destructive" id="btn-gw-stop" disabled>⏹ 停止</button>
          <button class="btn btn-secondary" id="btn-gw-restart" disabled>🔄 重启</button>
        </div>
      </div>

      <!-- 快捷链接 -->
      <div class="gateway-links">
        <a class="btn btn-secondary" id="btn-open-web" href="#"
           aria-disabled="true" style="pointer-events:none;opacity:.5;text-decoration:none">
          🌐 打开 Web UI
        </a>
        <button class="btn btn-secondary" id="btn-open-cli" disabled>
          💻 打开 CLI 终端
        </button>
      </div>

      <!-- 日志面板 -->
      <div class="log-panel">
        <div class="log-panel-header">
          <span>📋 日志输出</span>
          <button class="btn btn-ghost btn-sm" id="btn-clear-logs">清空</button>
        </div>
        <div class="log-content" id="log-content">
          <div class="log-line">等待 Gateway 启动...</div>
        </div>
      </div>
    </div>
  `;

  const dot = document.getElementById('gateway-dot');
  const statusText = document.getElementById('gateway-status-text');
  const statusMeta = document.getElementById('gateway-status-meta');
  const btnStart = document.getElementById('btn-gw-start');
  const btnStop = document.getElementById('btn-gw-stop');
  const btnRestart = document.getElementById('btn-gw-restart');
  const btnOpenWeb = document.getElementById('btn-open-web');
  const btnOpenCli = document.getElementById('btn-open-cli');
  const logContent = document.getElementById('log-content');
  const btnClearLogs = document.getElementById('btn-clear-logs');

  // ── 更新 UI 状态 ────────────────────────
  function updateUI(status) {
    const running = status.running;
    dot.className = `gateway-status-dot ${running ? 'running' : 'stopped'}`;
    statusText.textContent = running ? '● 运行中' : '○ 已停止';
    statusMeta.textContent = running
      ? `PID: ${status.pid}  |  已运行 ${formatUptime(status.uptime)}`
      : '点击「启动」按钮开始';
    btnStart.disabled = running;
    btnStop.disabled = !running;
    btnRestart.disabled = !running;
    btnOpenWeb.setAttribute('aria-disabled', status.web_ready ? 'false' : 'true');
    btnOpenWeb.style.pointerEvents = status.web_ready ? 'auto' : 'none';
    btnOpenWeb.style.opacity = status.web_ready ? '1' : '.5';
    btnOpenWeb.href = status.web_ready ? _gatewayWebUrl : '#';
    btnOpenWeb.title = status.web_ready ? status.web_url : 'Web 渠道尚未就绪';
    btnOpenCli.disabled = !running;
  }

  function appendLog(line) {
    // 清空占位消息
    const firstLine = logContent.querySelector('.log-line');
    if (firstLine && firstLine.textContent === '等待 Gateway 启动...') {
      logContent.innerHTML = '';
    }
    const el = document.createElement('div');
    el.className = 'log-line ' + classifyLog(line);
    el.textContent = line;
    logContent.appendChild(el);
    logContent.scrollTop = logContent.scrollHeight;
  }

  // ── 连接 WebSocket 日志流 ────────────────
  function connectLogWs() {
    if (_logWs) { _logWs.close(); _logWs = null; }
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/gateway/logs`);
    _logWs = ws;
    ws.onmessage = (e) => appendLog(e.data);
    ws.onclose = () => {
      // 断线重连
      setTimeout(connectLogWs, 2000);
    };
    ws.onerror = () => ws.close();
  }

  // ── 轮询状态 ─────────────────────────────
  async function pollStatus() {
    try {
      const status = await API.getGatewayStatus();
      updateUI(status);
    } catch (e) {
      statusText.textContent = '⚠️ 状态获取失败';
    }
  }

  // ── 按钮事件 ─────────────────────────────
  btnStart.addEventListener('click', async () => {
    btnStart.disabled = true;
    btnStart.textContent = '启动中...';
    try {
      const result = await API.startGateway();
      if (result.status === 'success') {
        Toast.success('Gateway 已启动');
        appendLog(`✅ ${result.message}`);
      } else {
        Toast.error(result.message);
      }
    } catch (e) {
      Toast.error('启动失败: ' + e.message);
    }
    btnStart.textContent = '▶ 启动';
    await pollStatus();
  });

  btnStop.addEventListener('click', async () => {
    if (!confirm('确定停止 Gateway 吗？')) return;
    try {
      const result = await API.stopGateway();
      Toast.info('Gateway 已停止');
      appendLog(`⏹ ${result.message}`);
    } catch (e) {
      Toast.error('停止失败: ' + e.message);
    }
    await pollStatus();
  });

  btnRestart.addEventListener('click', async () => {
    try {
      await API.stopGateway();
      appendLog('⏹ Gateway 已停止');
      await new Promise(r => setTimeout(r, 1000));
      const result = await API.startGateway();
      if (result.status === 'success') {
        Toast.success('Gateway 已重启');
        appendLog(`✅ ${result.message}`);
      }
    } catch (e) {
      Toast.error('重启失败: ' + e.message);
    }
    await pollStatus();
  });

  btnOpenCli.addEventListener('click', async () => {
    try {
      const result = await API.openCli();
      Toast.success('CLI 终端已打开');
    } catch (e) {
      Toast.error('打开 CLI 失败: ' + e.message);
    }
  });

  btnClearLogs.addEventListener('click', () => {
    logContent.innerHTML = '<div class="log-line">日志已清空</div>';
  });

  // ── 初始化 ───────────────────────────────
  await pollStatus();
  connectLogWs();

  // 启动状态轮询（每 5 秒）
  if (_statusPollTimer) clearInterval(_statusPollTimer);
  _statusPollTimer = setInterval(pollStatus, 5000);
}

// ── 工具函数 ───────────────────────────────
function formatUptime(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分${Math.round(seconds % 60)}秒`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}时${m}分`;
}

function classifyLog(line) {
  if (!line) return '';
  const l = line.toLowerCase();
  if (l.includes('错误') || l.includes('error') || l.includes('fail') || l.includes('异常'))
    return 'error';
  if (l.includes('警告') || l.includes('warn') || l.includes('warning'))
    return 'warn';
  if (l.includes('成功') || l.includes('success') || l.includes('✅') || l.includes('已启动') || l.includes('已注册'))
    return 'success';
  if (l.includes('[启动]') || l.includes('[mcp]') || l.includes('nanoclaw') || l.includes('='.repeat(10)))
    return 'highlight';
  return 'info';
}
    if (status.web_url) _gatewayWebUrl = status.web_url;
