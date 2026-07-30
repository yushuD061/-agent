/**
 * MCP 管理页面
 * 显示 MCP Server 卡片列表，支持添加/编辑/删除/开关
 */

async function renderMcp() {
  const container = document.getElementById('main-content');
  container.innerHTML = `
    <div class="page-header">
      <h1>🔌 MCP 服务</h1>
      <p>管理 Model Context Protocol (MCP) 服务器</p>
    </div>
    <div class="page-body">
      <div style="margin-bottom:16px">
        <button class="btn btn-primary" id="btn-add-mcp">+ 添加 MCP 服务器</button>
      </div>
      <div id="mcp-list">
        <div class="empty-state">
          <div class="empty-state-icon">🔌</div>
          <div class="empty-state-text">加载中...</div>
        </div>
      </div>
    </div>
  `;

  const listEl = document.getElementById('mcp-list');
  const btnAdd = document.getElementById('btn-add-mcp');

  // ── 加载列表 ─────────────────────────────
  async function loadList() {
    try {
      const data = await API.listMcpServers();
      const servers = data.servers || [];
      if (servers.length === 0) {
        listEl.innerHTML = `
          <div class="empty-state">
            <div class="empty-state-icon">🔌</div>
            <div class="empty-state-text">暂无 MCP 服务器</div>
            <div style="margin-top:8px;font-size:12px;color:var(--color-fg-muted)">
              点击上方按钮添加你的第一个 MCP 服务器
            </div>
          </div>
        `;
        return;
      }
      listEl.innerHTML = '';
      for (const svr of servers) {
        const card = document.createElement('div');
        card.className = 'mcp-card';
        card.innerHTML = `
          <div class="mcp-card-info">
            <div class="mcp-card-name">
              ${svr.name}
              <span class="badge ${svr.enabled ? 'badge-success' : 'badge-neutral'}">
                ${svr.configured ? (svr.enabled ? '已启用' : '已禁用') : '已发现未注册'}
              </span>
              ${svr.detected ? '<span class="badge badge-neutral">项目入口</span>' : ''}
            </div>
            <div class="mcp-card-command">${svr.command} ${(svr.args || []).join(' ')}</div>
            ${svr.description ? `<div class="mcp-card-desc">${svr.description}</div>` : ''}
            ${svr.env ? `<div class="mcp-card-desc" style="color:var(--color-fg-muted)">env: ${Object.keys(svr.env).join(', ')}</div>` : ''}
          </div>
          <div class="mcp-card-actions">
            ${svr.configured ? `
            <label class="toggle" data-name="${svr.name}">
              <input type="checkbox" ${svr.enabled ? 'checked' : ''} />
              <span class="toggle-slider"></span>
            </label>
            <button class="btn btn-ghost btn-sm btn-icon" data-action="edit" data-name="${svr.name}" title="编辑">✏️</button>
            <button class="btn btn-ghost btn-sm btn-icon" data-action="delete" data-name="${svr.name}" title="删除">🗑️</button>
            ` : ''}
          </div>
        `;
        listEl.appendChild(card);
      }

      // ── 事件绑定 ────────────────────────
      // Toggle 开关
      listEl.querySelectorAll('.toggle input').forEach(cb => {
        cb.addEventListener('change', async (e) => {
          const name = e.target.closest('.toggle').dataset.name;
          try {
            const result = await API.toggleMcpServer(name);
            if (result.enabled !== undefined) {
              // 更新 badge
              const card = e.target.closest('.mcp-card');
              const badge = card.querySelector('.badge');
              badge.className = `badge ${result.enabled ? 'badge-success' : 'badge-neutral'}`;
              badge.textContent = result.enabled ? '已启用' : '已禁用';
              Toast.success(`MCP "${name}" ${result.enabled ? '已启用' : '已禁用'}（重启 Gateway 生效）`);
            }
          } catch (err) {
            e.target.checked = !e.target.checked; // 恢复状态
            Toast.error('切换失败: ' + err.message);
          }
        });
      });

      // 编辑按钮
      listEl.querySelectorAll('[data-action="edit"]').forEach(btn => {
        btn.addEventListener('click', () => {
          const name = btn.dataset.name;
          const svr = servers.find(s => s.name === name);
          if (svr) openEditModal(svr);
        });
      });

      // 删除按钮
      listEl.querySelectorAll('[data-action="delete"]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const name = btn.dataset.name;
          if (!confirm(`确定要删除 MCP 服务器 "${name}" 吗？`)) return;
          try {
            await API.deleteMcpServer(name);
            Toast.success(`MCP "${name}" 已删除`);
            await loadList();
          } catch (err) {
            Toast.error('删除失败: ' + err.message);
          }
        });
      });
    } catch (err) {
      listEl.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⚠️</div>
          <div class="empty-state-text">加载失败: ${err.message}</div>
        </div>
      `;
    }
  }

  // ── 添加 Modal ───────────────────────────
  btnAdd.addEventListener('click', () => openAddModal(loadList));

  await loadList();
}

// ── JSON 解析（兼容 Cherry Studio 格式 + 裸格式） ────
function parseMcpJson(raw) {
  const data = JSON.parse(raw);

  // Cherry Studio 格式: { "mcpServers": { "name": { "command": ... } } }
  if (data.mcpServers && typeof data.mcpServers === 'object') {
    const entries = Object.entries(data.mcpServers);
    if (entries.length === 0) throw new Error('mcpServers 为空');
    const [name, config] = entries[0];
    if (!config.command) throw new Error(`服务器 "${name}" 缺少 command 字段`);
    return { name, config };
  }

  // 裸格式: { "command": "...", "args": [...], ... }
  if (!data.command) throw new Error('JSON 中缺少 command 字段');
  return { name: null, config: data };
}

// ── 从解析结果构建 API 参数 ─────────────────
function buildMcpPayload(config) {
  return {
    command: config.command,
    args: config.args || [],
    description: config.description || '',
    enabled: config.enabled !== false,
    env: config.env || undefined,
  };
}

// ── Modal: 添加 MCP Server ─────────────────
function openAddModal(onSuccess) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h2>添加 MCP 服务器</h2>
        <p>支持 Cherry Studio JSON 格式（含 mcpServers 包裹）或裸格式</p>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">服务器名称（裸格式时必填）</label>
          <input class="form-input form-input-mono" id="mcp-add-name"
                 placeholder="my-server" />
        </div>
        <div class="form-group">
          <label class="form-label">配置 JSON</label>
          <textarea class="code-editor" id="mcp-add-json" rows="10"
            placeholder='{\n  "command": "npx",\n  "args": ["-y", "package-name"],\n  "description": "..."\n}'>${getMcpExampleJson()}</textarea>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" id="mcp-add-cancel">取消</button>
        <button class="btn btn-primary" id="mcp-add-confirm">添加</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const nameInput = document.getElementById('mcp-add-name');
  const jsonInput = document.getElementById('mcp-add-json');

  setTimeout(() => nameInput.focus(), 100);

  document.getElementById('mcp-add-cancel').addEventListener('click', () => overlay.remove());
  document.getElementById('mcp-add-confirm').addEventListener('click', async () => {
    try {
      const { name, config } = parseMcpJson(jsonInput.value);
      const finalName = name || nameInput.value.trim();
      if (!finalName) { Toast.error('请输入服务器名称'); return; }
      await API.addMcpServer(finalName, buildMcpPayload(config));
      Toast.success(`MCP "${finalName}" 已添加`);
      overlay.remove();
      if (onSuccess) await onSuccess();
    } catch (err) {
      Toast.error(err.message);
    }
  });

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

// ── Modal: 编辑 MCP Server ─────────────────
function openEditModal(server) {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-header">
        <h2>编辑 MCP 服务器</h2>
        <p>${server.name}</p>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label class="form-label">配置 JSON</label>
          <textarea class="code-editor" id="mcp-edit-json" rows="12">${JSON.stringify({
            command: server.command,
            args: server.args,
            description: server.description,
            enabled: server.enabled,
            ...(server.env ? { env: server.env } : {}),
          }, null, 2)}</textarea>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" id="mcp-edit-cancel">取消</button>
        <button class="btn btn-primary" id="mcp-edit-confirm">保存</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const jsonInput = document.getElementById('mcp-edit-json');

  document.getElementById('mcp-edit-cancel').addEventListener('click', () => overlay.remove());
  document.getElementById('mcp-edit-confirm').addEventListener('click', async () => {
    try {
      const { config } = parseMcpJson(jsonInput.value);
      await API.updateMcpServer(server.name, buildMcpPayload(config));
      Toast.success(`MCP "${server.name}" 已更新`);
      overlay.remove();
      renderMcp();
    } catch (err) {
      Toast.error(err.message);
    }
  });

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.remove();
  });
}

// ── JSON 示例（同时展示 Cherry Studio 格式） ────
function getMcpExampleJson() {
  return JSON.stringify({
    command: "npx",
    args: ["-y", "@modelcontextprotocol/server-filesystem", "./"],
    description: "文件系统操作",
  }, null, 2);
}
