/**
 * 设置页面 — API 配置管理
 * API Key / URL / Model / 配置 Profile / 打开工作区
 */

// ── Profile 管理 ───────────────────────────
const PROFILE_KEY = 'nanoclaw_profiles';

function loadProfiles() {
  try {
    return JSON.parse(localStorage.getItem(PROFILE_KEY)) || {};
  } catch { return {}; }
}

function saveProfiles(profiles) {
  localStorage.setItem(PROFILE_KEY, JSON.stringify(profiles));
}

async function renderSettings() {
  const container = document.getElementById('main-content');
  container.innerHTML = `
    <div class="page-header">
      <h1>⚙️ 设置</h1>
      <p>配置 API 连接信息和 NanoClaw 工作区</p>
    </div>
    <div class="page-body">
      <div class="card">
        <div class="card-title">API 配置</div>
        <div class="form-group">
          <label class="form-label">配置 Profile</label>
          <div class="profile-selector">
            <select class="profile-select" id="profile-select">
              <option value="__new">+ 新建配置</option>
            </select>
            <button class="btn btn-secondary btn-sm" id="btn-save-profile">💾 保存</button>
            <button class="btn btn-ghost btn-sm" id="btn-delete-profile">🗑️ 删除</button>
          </div>
        </div>
        <div class="divider"></div>
        <div class="form-group">
          <label class="form-label">API 密钥</label>
          <div class="api-key-display">
            <div class="api-key-input-wrapper">
              <input class="form-input form-input-mono" id="input-api-key" type="password"
                     placeholder="sk-..." autocomplete="off" />
              <button class="api-key-toggle" id="btn-toggle-key" title="显示/隐藏">👁️</button>
            </div>
            <button class="btn btn-secondary btn-sm" id="btn-open-explorer" title="打开工作区文件夹">📂</button>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label class="form-label">API 地址</label>
            <input class="form-input form-input-mono" id="input-base-url"
                   placeholder="https://api.deepseek.com/v1" />
          </div>
          <div class="form-group">
            <label class="form-label">模型名称</label>
            <input class="form-input form-input-mono" id="input-model"
                   placeholder="deepseek-v4-flash" />
          </div>
        </div>
        <div style="margin-top:16px;display:flex;gap:8px">
          <button class="btn btn-primary" id="btn-save-config">💾 保存配置</button>
        </div>
      </div>

      <!-- Python 环境 -->
      <div class="card">
        <div class="card-title">🐍 Python 环境</div>
        <p style="font-size:12.5px;color:var(--color-fg-secondary);margin-bottom:12px">
          选择用于启动 Gateway 的 Python 解释器。移机后首次使用请重新配置。
        </p>
        <div class="form-row">
          <div class="form-group" style="flex:1">
            <label class="form-label">Python 可执行文件</label>
            <select class="profile-select" id="python-env-select">
              <option value="">加载中...</option>
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">&nbsp;</label>
            <button class="btn btn-secondary" id="btn-scan-python">🔍 扫描</button>
          </div>
        </div>
        <div id="python-env-detail" style="font-size:12px;color:var(--color-fg-muted);margin-top:4px"></div>
        <div style="margin-top:12px">
          <label class="form-label">或手动输入路径</label>
          <div class="form-row">
            <input class="form-input form-input-mono" id="python-path-manual" placeholder="例如: /usr/bin/python3 或 C:\Python311\python.exe" style="flex:1" />
            <button class="btn btn-primary btn-sm" id="btn-set-python">应用</button>
          </div>
        </div>
      </div>
    </div>
  `;

  // ── 加载当前配置 ─────────────────────────
  let currentConfig = {};
  try {
    currentConfig = await API.getConfig();
  } catch (e) {
    Toast.error('读取配置失败: ' + e.message);
  }

  const inputKey = document.getElementById('input-api-key');
  const inputUrl = document.getElementById('input-base-url');
  const inputModel = document.getElementById('input-model');
  const profileSelect = document.getElementById('profile-select');
  const btnToggleKey = document.getElementById('btn-toggle-key');
  const btnSaveConfig = document.getElementById('btn-save-config');
  const btnSaveProfile = document.getElementById('btn-save-profile');
  const btnDeleteProfile = document.getElementById('btn-delete-profile');
  const btnOpenExplorer = document.getElementById('btn-open-explorer');

  // 填充当前值
  inputKey.value = currentConfig.api_key_raw || '';
  inputUrl.value = currentConfig.base_url || '';
  inputModel.value = currentConfig.model || '';

  // ── Profile 下拉 ─────────────────────────
  function refreshProfileSelect(profiles, selected) {
    const current = profileSelect.value;
    profileSelect.innerHTML = '<option value="__new">+ 新建配置</option>';
    for (const name of Object.keys(profiles)) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      if (name === selected || name === current) opt.selected = true;
      profileSelect.appendChild(opt);
    }
  }

  let profiles = loadProfiles();
  // 如果有配置且无 profile，自动创建默认
  if (currentConfig.api_key_raw && Object.keys(profiles).length === 0) {
    profiles['默认'] = {
      api_key: currentConfig.api_key_raw,
      base_url: currentConfig.base_url,
      model: currentConfig.model,
    };
    saveProfiles(profiles);
  }
  refreshProfileSelect(profiles, '默认');

  // ── 切换 API Key 显示 ────────────────────
  let keyVisible = false;
  btnToggleKey.addEventListener('click', () => {
    keyVisible = !keyVisible;
    inputKey.type = keyVisible ? 'text' : 'password';
    btnToggleKey.textContent = keyVisible ? '🙈' : '👁️';
  });

  // ── 保存配置到 NanoClaw ──────────────────
  btnSaveConfig.addEventListener('click', async () => {
    const apiKey = inputKey.value.trim();
    const baseUrl = inputUrl.value.trim();
    const model = inputModel.value.trim();

    if (!apiKey) { Toast.error('API 密钥不能为空'); return; }
    if (apiKey.length < 8) { Toast.error('API 密钥长度不足'); return; }

    try {
      // 先单独更新 API Key（有校验）
      const keyResult = await API.updateApiKey(apiKey);
      if (keyResult.status !== 'success') {
        Toast.error('API Key 更新失败: ' + (keyResult.detail || ''));
        return;
      }
      // 再更新其他字段
      const updateData = {};
      if (baseUrl) updateData.base_url = baseUrl;
      if (model) updateData.model = model;
      if (Object.keys(updateData).length > 0) {
        await API.updateConfig(updateData);
      }
      Toast.success('配置已保存');
    } catch (e) {
      Toast.error('保存失败: ' + e.message);
    }
  });

  // ── 保存当前为 Profile ───────────────────
  btnSaveProfile.addEventListener('click', () => {
    const name = prompt('请输入配置名称：');
    if (!name || !name.trim()) return;
    profiles[name.trim()] = {
      api_key: inputKey.value.trim(),
      base_url: inputUrl.value.trim(),
      model: inputModel.value.trim(),
    };
    saveProfiles(profiles);
    refreshProfileSelect(profiles, name.trim());
    Toast.success(`配置 "${name.trim()}" 已保存`);
  });

  // ── 删除 Profile ─────────────────────────
  btnDeleteProfile.addEventListener('click', () => {
    const selected = profileSelect.value;
    if (selected === '__new') { Toast.info('请先选择要删除的配置'); return; }
    if (!confirm(`确定删除配置 "${selected}" 吗？`)) return;
    delete profiles[selected];
    saveProfiles(profiles);
    refreshProfileSelect(profiles, Object.keys(profiles)[0] || null);
    // 清除表单
    inputKey.value = '';
    inputUrl.value = '';
    inputModel.value = '';
    Toast.success(`配置 "${selected}" 已删除`);
  });

  // ── 切换 Profile ─────────────────────────
  profileSelect.addEventListener('change', () => {
    const selected = profileSelect.value;
    if (selected === '__new') {
      inputKey.value = '';
      inputUrl.value = '';
      inputModel.value = '';
      return;
    }
    const profile = profiles[selected];
    if (profile) {
      inputKey.value = profile.api_key || '';
      inputUrl.value = profile.base_url || '';
      inputModel.value = profile.model || '';
    }
  });

  // ── 打开工作区 ───────────────────────────
  btnOpenExplorer.addEventListener('click', async () => {
    try {
      const result = await API.openExplorer();
      Toast.success(result.message || '已打开工作区');
    } catch (e) {
      Toast.error('打开失败: ' + e.message);
    }
  });

  // ── Python 环境选择 ───────────────────────
  const pythonSelect = document.getElementById('python-env-select');
  const pythonDetail = document.getElementById('python-env-detail');
  const pythonManual = document.getElementById('python-path-manual');
  const btnScan = document.getElementById('btn-scan-python');
  const btnSetPython = document.getElementById('btn-set-python');

  async function loadPythonEnvs() {
    try {
      const data = await (await fetch('/api/util/python-envs')).json();
      const envs = data.environments || [];
      const current = data.current || '';
      pythonSelect.innerHTML = '<option value="">（请选择）</option>';
      for (const env of envs) {
        const opt = document.createElement('option');
        opt.value = env.path;
        opt.textContent = `${env.label}  (${env.version})`;
        if (env.path === current || (current === '' && env.type === 'current')) {
          opt.selected = true;
        }
        pythonSelect.appendChild(opt);
      }
      // 显示当前选中详情
      updatePythonDetail(current || (envs.find(e => e.type === 'current')?.path || ''));
      pythonManual.value = current;
    } catch (e) {
      pythonSelect.innerHTML = '<option value="">扫描失败</option>';
      pythonDetail.textContent = '❌ ' + e.message;
    }
  }

  function updatePythonDetail(path) {
    if (path) {
      pythonDetail.textContent = '当前选择: ' + path;
    } else {
      pythonDetail.textContent = '未选择（将使用默认 Python）';
    }
  }

  // 下拉选择
  pythonSelect.addEventListener('change', async () => {
    const path = pythonSelect.value;
    pythonManual.value = path;
    updatePythonDetail(path);
    if (path) {
      try {
        await fetch('/api/launcher/config', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({python_path: path}),
        });
        Toast.success('Python 环境已切换');
      } catch (e) {
        Toast.error('保存失败: ' + e.message);
      }
    }
  });

  // 手动输入
  btnSetPython.addEventListener('click', async () => {
    const path = pythonManual.value.trim();
    if (!path) { Toast.error('请输入 Python 路径'); return; }
    try {
      await fetch('/api/launcher/config', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({python_path: path}),
      });
      updatePythonDetail(path);
      Toast.success('Python 路径已设置');
      await loadPythonEnvs(); // 刷新下拉
    } catch (e) {
      Toast.error('保存失败: ' + e.message);
    }
  });

  // 扫描按钮
  btnScan.addEventListener('click', async () => {
    btnScan.textContent = '扫描中...';
    btnScan.disabled = true;
    await loadPythonEnvs();
    btnScan.textContent = '🔍 扫描';
    btnScan.disabled = false;
  });

  // 初始化加载
  await loadPythonEnvs();
}
