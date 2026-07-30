(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const copy = {
    zh: {
      'nav.conversations':'对话','portal.label':'客户门户','portal.title':'我的询盘',
      'chat.new':'新建询盘','chat.search':'搜索询盘','chat.recent':'最近询盘',
      'chat.inquiry':'询盘','status.progress':'处理中','chat.delete':'删除询盘',
      'assistant.identity':'NanoClaw 客户服务顾问','welcome.title':'开始一项新询盘',
      'welcome.subtitle':'请提供产品、数量、目的地、贸易术语和交期。销售团队会在人工审核后继续跟进。',
      'chat.inputLabel':'询盘内容','chat.placeholder':'描述您的采购需求…','chat.send':'发送询盘',
      'portal.privacy':'询盘内容和报价将由销售团队审核确认。',
      'prompt.usbcLabel':'USB-C 数据线','prompt.usbcText':'采购 1,000 条 USB-C 数据线，CIF Hamburg，30 天内交货。',
      'prompt.tabletLabel':'工业平板','prompt.tabletText':'需要 500 台工业平板，交付至 Rotterdam。',
      'chat.newTitle':'新建客户询盘','chat.newHint':'创建新的询盘对话，便于销售团队跟进您的需求。',
      'common.cancel':'取消','common.create':'创建','common.close':'关闭','time.today':'今天','time.yesterday':'昨天',
      'auth.account':'客户账号','auth.loginButton':'登录 / 注册','auth.title':'客户登录','auth.hint':'登录后可恢复本账号的历史对话。',
      'auth.registerTitle':'创建客户账号','auth.registerHint':'注册后将自动登录，并按账号隔离您的询盘和任务。','auth.registerButton':'注册账号','auth.backToLogin':'返回登录','auth.register':'注册并登录',
      'auth.email':'邮箱','auth.password':'密码','auth.confirmPassword':'确认密码','auth.login':'登录','auth.logout':'退出',
      'auth.emailPlaceholder':'name@example.com','auth.passwordPlaceholder':'请输入至少 12 位密码','auth.confirmPasswordPlaceholder':'再次输入密码','auth.passwordRule':'密码至少 12 位，请勿与其他网站共用。','auth.security':'登录信息将通过安全连接提交。',
      'auth.failed':'登录失败，请检查账号和密码。','auth.registerFailed':'注册失败，请检查邮箱和密码。','auth.passwordMismatch':'两次输入的密码不一致。','auth.accountExists':'该邮箱已经注册，请直接登录。','auth.registrationDisabled':'当前未开放注册。','task.title':'询盘处理进程',
      'task.instruction':'补充或修改要求','task.instructionPlaceholder':'例如：数量改为 500，目的地改为 Hamburg',
      'task.apply':'提交并从有效断点继续','task.pause':'暂停','task.resume':'恢复','task.retry':'重试','task.cancel':'取消',
      'task.confirm':'确认报价','task.waiting':'等待处理','task.noAction':'暂无需要您处理的事项',
      'task.missing':'请补充：','task.artifacts':'已批准文件','task.noArtifact':'暂无可下载文件','connection.connecting':'正在连接'
    },
    en: {
      'nav.conversations':'Inquiries','portal.label':'CUSTOMER PORTAL','portal.title':'My inquiries',
      'chat.new':'New inquiry','chat.search':'Search inquiries','chat.recent':'Recent inquiries',
      'chat.inquiry':'INQUIRY','status.progress':'In progress','chat.delete':'Delete inquiry',
      'assistant.identity':'NanoClaw Customer Sales Assistant','welcome.title':'Start a new inquiry',
      'welcome.subtitle':'Share the product, quantity, destination, Incoterm, and delivery date. Our sales team will review and follow up.',
      'chat.inputLabel':'Inquiry details','chat.placeholder':'Tell us what you need…','chat.send':'Send inquiry',
      'portal.privacy':'Inquiry details and quotations are reviewed by our sales team.',
      'prompt.usbcLabel':'USB-C data cables','prompt.usbcText':'We need 1,000 USB-C data cables, CIF Hamburg, with delivery within 30 days.',
      'prompt.tabletLabel':'Industrial tablets','prompt.tabletText':'We need 500 industrial tablets delivered to Rotterdam.',
      'chat.newTitle':'New customer inquiry','chat.newHint':'Create a new inquiry so our sales team can follow up on your requirements.',
      'common.cancel':'Cancel','common.create':'Create','common.close':'Close','time.today':'Today','time.yesterday':'Yesterday',
      'auth.account':'Customer account','auth.loginButton':'Sign in / Register','auth.title':'Customer login','auth.hint':'Sign in to restore your account conversations.',
      'auth.registerTitle':'Create customer account','auth.registerHint':'After registration you will be signed in, with inquiries isolated to your account.','auth.registerButton':'Register','auth.backToLogin':'Back to sign in','auth.register':'Register and sign in',
      'auth.email':'Email','auth.password':'Password','auth.confirmPassword':'Confirm password','auth.login':'Sign in','auth.logout':'Sign out',
      'auth.emailPlaceholder':'name@example.com','auth.passwordPlaceholder':'Enter at least 12 characters','auth.confirmPasswordPlaceholder':'Enter the password again','auth.passwordRule':'Use at least 12 characters and do not reuse another site password.','auth.security':'Your sign-in details are submitted over a secure connection.',
      'auth.failed':'Sign-in failed. Check your credentials.','auth.registerFailed':'Registration failed. Check the email and password.','auth.passwordMismatch':'The passwords do not match.','auth.accountExists':'This email is already registered. Please sign in.','auth.registrationDisabled':'Registration is currently unavailable.','task.title':'Inquiry progress',
      'task.instruction':'Add or change a requirement','task.instructionPlaceholder':'Example: change quantity to 500 and destination to Hamburg',
      'task.apply':'Submit and resume from checkpoint','task.pause':'Pause','task.resume':'Resume','task.retry':'Retry','task.cancel':'Cancel',
      'task.confirm':'Confirm quotation','task.waiting':'Waiting','task.noAction':'Nothing needs your attention',
      'task.missing':'Please provide: ','task.artifacts':'Approved files','task.noArtifact':'No approved file yet','connection.connecting':'Connecting'
    },
    de: {
      'nav.conversations':'Anfragen','portal.label':'KUNDENPORTAL','portal.title':'Meine Anfragen',
      'chat.new':'Neue Anfrage','chat.search':'Anfragen suchen','chat.recent':'Letzte Anfragen',
      'chat.inquiry':'ANFRAGE','status.progress':'In Bearbeitung','chat.delete':'Anfrage löschen',
      'assistant.identity':'NanoClaw Kundenberater','welcome.title':'Neue Anfrage starten',
      'welcome.subtitle':'Nennen Sie Produkt, Menge, Zielort, Incoterm und Liefertermin. Unser Vertrieb prüft die Angaben und meldet sich.',
      'chat.inputLabel':'Anfragedetails','chat.placeholder':'Beschreiben Sie Ihren Bedarf…','chat.send':'Anfrage senden',
      'portal.privacy':'Anfragedaten und Angebote werden von unserem Vertrieb geprüft.',
      'prompt.usbcLabel':'USB-C-Datenkabel','prompt.usbcText':'Wir benötigen 1.000 USB-C-Datenkabel, CIF Hamburg, Lieferung innerhalb von 30 Tagen.',
      'prompt.tabletLabel':'Industrie-Tablets','prompt.tabletText':'Wir benötigen 500 Industrie-Tablets mit Lieferung nach Rotterdam.',
      'chat.newTitle':'Neue Kundenanfrage','chat.newHint':'Erstellen Sie eine Anfrage, damit unser Vertrieb Ihren Bedarf bearbeiten kann.',
      'common.cancel':'Abbrechen','common.create':'Erstellen','common.close':'Schließen','time.today':'Heute','time.yesterday':'Gestern',
      'auth.account':'Kundenkonto','auth.loginButton':'Anmelden / Registrieren','auth.title':'Kundenanmeldung','auth.hint':'Melden Sie sich an, um Ihre Anfragen wiederherzustellen.',
      'auth.registerTitle':'Kundenkonto erstellen','auth.registerHint':'Nach der Registrierung werden Sie angemeldet; Anfragen bleiben Ihrem Konto zugeordnet.','auth.registerButton':'Registrieren','auth.backToLogin':'Zur Anmeldung','auth.register':'Registrieren und anmelden',
      'auth.email':'E-Mail','auth.password':'Passwort','auth.confirmPassword':'Passwort bestätigen','auth.login':'Anmelden','auth.logout':'Abmelden',
      'auth.emailPlaceholder':'name@example.com','auth.passwordPlaceholder':'Mindestens 12 Zeichen eingeben','auth.confirmPasswordPlaceholder':'Passwort erneut eingeben','auth.passwordRule':'Mindestens 12 Zeichen verwenden und kein Passwort anderer Websites wiederverwenden.','auth.security':'Ihre Anmeldedaten werden über eine sichere Verbindung übertragen.',
      'auth.failed':'Anmeldung fehlgeschlagen. Prüfen Sie Ihre Zugangsdaten.','auth.registerFailed':'Registrierung fehlgeschlagen. Prüfen Sie E-Mail und Passwort.','auth.passwordMismatch':'Die Passwörter stimmen nicht überein.','auth.accountExists':'Diese E-Mail ist bereits registriert. Bitte anmelden.','auth.registrationDisabled':'Die Registrierung ist derzeit nicht verfügbar.','task.title':'Anfragefortschritt',
      'task.instruction':'Anforderung ergänzen oder ändern','task.instructionPlaceholder':'Beispiel: Menge 500, Zielort Hamburg',
      'task.apply':'Senden und ab Prüfpunkt fortsetzen','task.pause':'Pausieren','task.resume':'Fortsetzen','task.retry':'Wiederholen','task.cancel':'Abbrechen',
      'task.confirm':'Angebot bestätigen','task.waiting':'Warten','task.noAction':'Keine Aktion erforderlich',
      'task.missing':'Bitte ergänzen: ','task.artifacts':'Freigegebene Dateien','task.noArtifact':'Noch keine freigegebene Datei','connection.connecting':'Verbindung wird hergestellt'
    }
  };

  const stepLabels = {
    zh:{inquiry_structuring:'整理询盘',missing_information:'核对缺失信息',product_matching:'匹配产品',inventory_check:'核对供货',commercial_terms:'核对商务条款',quote_calculating:'计算报价',quote_drafting:'准备报价',internal_review:'销售审核',customer_confirmation:'客户确认',follow_up:'后续跟进'},
    en:{inquiry_structuring:'Structure inquiry',missing_information:'Check missing details',product_matching:'Match product',inventory_check:'Check availability',commercial_terms:'Check commercial terms',quote_calculating:'Calculate quotation',quote_drafting:'Prepare quotation',internal_review:'Sales review',customer_confirmation:'Customer confirmation',follow_up:'Follow-up'},
    de:{inquiry_structuring:'Anfrage strukturieren',missing_information:'Fehlende Angaben',product_matching:'Produkt abgleichen',inventory_check:'Verfügbarkeit prüfen',commercial_terms:'Handelsbedingungen',quote_calculating:'Angebot berechnen',quote_drafting:'Angebot vorbereiten',internal_review:'Vertriebsprüfung',customer_confirmation:'Kundenbestätigung',follow_up:'Nachverfolgung'}
  };
  const statusLabels = {
    zh:{queued:'排队中',running:'处理中',pause_requested:'正在安全暂停',paused:'已暂停',waiting_input:'等待您补充',waiting_review:'等待人工确认',replanning:'正在调整计划',retry_wait:'等待重试',failed:'处理失败',completed:'已完成',cancelled:'已取消'},
    en:{queued:'Queued',running:'In progress',pause_requested:'Pausing safely',paused:'Paused',waiting_input:'Waiting for your input',waiting_review:'Waiting for review',replanning:'Replanning',retry_wait:'Waiting to retry',failed:'Failed',completed:'Completed',cancelled:'Cancelled'},
    de:{queued:'Eingereiht',running:'In Bearbeitung',pause_requested:'Sicheres Pausieren',paused:'Pausiert',waiting_input:'Wartet auf Ihre Angaben',waiting_review:'Wartet auf Prüfung',replanning:'Plan wird angepasst',retry_wait:'Wartet auf Wiederholung',failed:'Fehlgeschlagen',completed:'Abgeschlossen',cancelled:'Abgebrochen'}
  };

  const browserLanguage = (navigator.language || 'en').toLowerCase();
  let lang = localStorage.getItem('nanoclaw-customer-language')
    || (browserLanguage.startsWith('zh') ? 'zh' : browserLanguage.startsWith('de') ? 'de' : 'en');
  let conversations = [];
  let messagesByConversation = {};
  let active = '';
  let socket = null;
  let reconnectTimer = null;
  let authenticated = false;
  let authMode = 'login';
  let anonymousMode = false;
  let connectionOnline = false;
  let csrfToken = '';
  let tasks = [];
  let activeTask = null;
  const welcomeMarkup = $('messages').innerHTML;
  const t = key => copy[lang]?.[key] || copy.en[key] || key;
  const taskStep = key => stepLabels[lang]?.[key] || key || '—';
  const taskStatus = value => statusLabels[lang]?.[value] || value;
  const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, character => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[character]);
  const cookieValue = name => document.cookie.split('; ').find(item => item.startsWith(`${name}=`))?.split('=').slice(1).join('=') || '';

  const syncComposerState = () => {
    const available = connectionOnline && Boolean(active);
    $('inquiryInput').disabled = !available;
    $('inquiryForm').querySelector('button[type="submit"]').disabled = !available;
  };

  const createAnonymousConversation = () => {
    const conversation = {id:crypto.randomUUID(),title:t('chat.newTitle'),dayKey:'time.today',version:1};
    conversations.unshift(conversation);
    messagesByConversation[conversation.id]=[];
    active=conversation.id;
    $('conversationTitle').textContent=conversation.title;
    renderList();renderMessages();syncComposerState();
    return conversation;
  };

  const customerApi = async (path, options = {}) => {
    const headers = {...(options.headers || {})};
    if (csrfToken && !['GET','HEAD'].includes((options.method || 'GET').toUpperCase())) {
      headers['X-CSRF-Token'] = decodeURIComponent(csrfToken);
    }
    const response = await fetch(path, {credentials:'same-origin', ...options, headers});
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `HTTP ${response.status}`);
    return response.status === 204 ? null : response.json();
  };

  const updateAccountButton = () => {
    const label = t(authenticated ? 'auth.account' : 'auth.loginButton');
    $('customerAccountLabel').textContent = label;
    $('customerAccountLabel').dataset.i18n = authenticated ? 'auth.account' : 'auth.loginButton';
    $('customerAccount').setAttribute('aria-label', label);
  };

  const setAuthMode = mode => {
    authMode = mode === 'register' ? 'register' : 'login';
    const registering = authMode === 'register';
    const titleKey = registering ? 'auth.registerTitle' : 'auth.title';
    const hintKey = registering ? 'auth.registerHint' : 'auth.hint';
    const toggleKey = registering ? 'auth.backToLogin' : 'auth.registerButton';
    const submitKey = registering ? 'auth.register' : 'auth.login';
    $('customerAuthTitle').dataset.i18n=titleKey;$('customerAuthTitle').textContent=t(titleKey);
    $('customerAuthStatus').dataset.i18n=hintKey;$('customerAuthStatus').textContent=t(hintKey);$('customerAuthStatus').dataset.state='neutral';
    $('customerAuthConfirmField').hidden=!registering;$('customerAuthConfirmPassword').required=registering;
    $('customerAuthRule').hidden=!registering;
    $('customerAuthPassword').autocomplete=registering?'new-password':'current-password';
    $('customerAuthModeToggle').dataset.i18n=toggleKey;$('customerAuthModeToggle').textContent=t(toggleKey);
    $('customerAuthSubmit').dataset.i18n=submitKey;$('customerAuthSubmit').textContent=t(submitKey);$('customerAuthSubmit').value=authMode;
  };

  const authErrorKey = (code, registering) => ({
    customer_identity_conflict:'auth.accountExists',
    customer_registration_disabled:'auth.registrationDisabled',
    customer_input_invalid:registering?'auth.registerFailed':'auth.failed',
  }[code] || (registering?'auth.registerFailed':'auth.failed'));

  const appendMessage = (role, value) => {
    const row = document.createElement('article');
    row.className = `portal-message-row ${role}`;
    const avatar = document.createElement('span');
    const isAssistant = role === 'assistant';
    avatar.className = `portal-message-avatar ${isAssistant ? 'brand-avatar' : 'customer-avatar'}`;
    avatar.setAttribute('role','img');
    avatar.setAttribute('aria-label', isAssistant ? t('assistant.identity') : (lang === 'zh' ? '客户' : lang === 'de' ? 'Kunde' : 'Customer'));
    avatar.innerHTML = isAssistant
      ? '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M10.5 11.5 7 7m14.5 4.5L25 7M10 12c-3.7.7-5.2 4.8-3 7.8 1.8 2.5 5.5 2.8 8 .7m7-8.5c3.7.7 5.2 4.8 3 7.8-1.8 2.5-5.5 2.8-8 .7"/><path d="M11 12.5c0-2.4 2.2-4.5 5-4.5s5 2.1 5 4.5v7c0 2.7-2.2 4.8-5 4.8s-5-2.1-5-4.8z"/><circle cx="14" cy="14" r="1"/><circle cx="18" cy="14" r="1"/></svg>'
      : '<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="11.5" r="5"/><path d="M7.5 26c.8-5.5 4-8.3 8.5-8.3s7.7 2.8 8.5 8.3"/></svg>';
    const message = document.createElement('div');
    message.className = `preview-message ${role}`;
    if (isAssistant) {
      message.classList.add('markdown-body');
      message.innerHTML = window.NanoClawMarkdown?.render(value) || '';
    } else message.textContent = value;
    if (isAssistant) row.append(avatar, message); else row.append(message, avatar);
    $('messages').append(row);
  };

  const renderMessages = () => {
    const messages = messagesByConversation[active] || [];
    $('messages').innerHTML = messages.length ? '' : welcomeMarkup;
    messages.forEach(item => appendMessage(item.role,item.content));
    $('messages').scrollTop = $('messages').scrollHeight;
  };

  const renderList = () => {
    const query = $('conversationSearch').value.trim().toLowerCase();
    const shown = conversations.filter(item => !query || item.title.toLowerCase().includes(query));
    $('conversationList').innerHTML = shown.map(item => `<button class="conversation-item${item.id === active ? ' active' : ''}" data-id="${escapeHtml(item.id)}"><span class="conversation-avatar">N</span><span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(t(item.dayKey || 'time.today'))}</small></span></button>`).join('');
    $('chatCount').textContent = String(conversations.length);
    $('conversationList').querySelectorAll('button').forEach(button => {
      button.onclick = async () => {
        active = button.dataset.id;
        $('conversationTitle').textContent = conversations.find(item => item.id === active)?.title || t('portal.title');
        await loadServerMessages(active);
        await loadCustomerTasks();
        renderList();
        renderMessages();
        syncComposerState();
      };
    });
  };

  const loadServerMessages = async conversationId => {
    if (!authenticated || !conversationId) return;
    const payload = await customerApi(`/api/customer/conversations/${encodeURIComponent(conversationId)}/messages?limit=200`);
    messagesByConversation[conversationId] = payload.items
      .filter(item => ['user','assistant'].includes(item.role))
      .map(item => ({role:item.role,content:typeof item.content === 'string' ? item.content : JSON.stringify(item.content)}));
  };

  const renderCustomerTask = () => {
    const panel = $('customerTaskPanel');
    panel.hidden = !activeTask;
    if (!activeTask) return;
    $('customerTaskVersion').textContent = `Plan v${activeTask.active_plan_version}`;
    $('customerTaskStatus').textContent = taskStatus(activeTask.status);
    const summary = activeTask.summary || {};
    $('customerTaskSummary').innerHTML = Object.entries(summary).map(([key,value]) => `<span><b>${escapeHtml(key)}</b>${escapeHtml(value)}</span>`).join('');
    $('customerTaskSteps').innerHTML = (activeTask.steps || []).map(step => `<li class="${escapeHtml(step.status)}"><i>${Number(step.ordinal)||0}</i><span>${escapeHtml(taskStep(step.step_key))}</span><small>${escapeHtml(taskStatus(step.status))}</small></li>`).join('');
    const controls = [];
    if (['queued','running','retry_wait'].includes(activeTask.status)) controls.push(`<button data-command="pause">${t('task.pause')}</button>`);
    if (['paused','pause_requested'].includes(activeTask.status)) controls.push(`<button data-command="resume">${t('task.resume')}</button>`);
    if (['failed','retry_wait'].includes(activeTask.status)) controls.push(`<button data-command="retry">${t('task.retry')}</button>`);
    if (!['completed','cancelled'].includes(activeTask.status)) controls.push(`<button data-command="cancel" class="danger">${t('task.cancel')}</button>`);
    (activeTask.human_actions || []).filter(action => action.status === 'pending').forEach(action => {
      const missing = action.payload?.missing_fields || [];
      if (action.step_key === 'customer_confirmation') {
        controls.push(`<button data-decision="confirm" data-action-id="${escapeHtml(action.action_id)}">${escapeHtml(t('task.confirm'))}</button>`);
      } else if (missing.length) {
        controls.push(`<small>${escapeHtml(t('task.missing'))}${missing.map(escapeHtml).join(', ')}</small>`);
      }
    });
    const artifacts = (activeTask.artifacts || []).map(item => `<a href="/api/customer/tasks/${encodeURIComponent(activeTask.task_id)}/artifacts/${encodeURIComponent(item.artifact_id)}" target="_blank" rel="noopener">${escapeHtml(item.file_name)}</a>`);
    if (artifacts.length) controls.push(`<small>${escapeHtml(t('task.artifacts'))}</small>${artifacts.join('')}`);
    $('customerTaskActions').innerHTML = controls.join('') || `<small>${escapeHtml(t('task.noAction'))}</small>`;
    $('customerTaskActions').querySelectorAll('[data-command]').forEach(button => button.onclick = () => customerTaskCommand(button.dataset.command));
    $('customerTaskActions').querySelectorAll('[data-decision]').forEach(button => button.onclick = () => customerTaskDecision(button.dataset.actionId,button.dataset.decision));
  };

  const loadCustomerTasks = async () => {
    if (!authenticated || !active) { tasks=[];activeTask=null;renderCustomerTask();return; }
    const payload = await customerApi(`/api/customer/tasks?conversation_id=${encodeURIComponent(active)}`);
    tasks = payload.items || [];
    activeTask = tasks.find(item => !['completed','cancelled'].includes(item.status)) || tasks[0] || null;
    renderCustomerTask();
  };

  const customerTaskCommand = async command => {
    if (!activeTask) return;
    activeTask = await customerApi(`/api/customer/tasks/${activeTask.task_id}/commands/${command}`, {
      method:'POST',headers:{'If-Match':activeTask.etag,'Idempotency-Key':crypto.randomUUID()},body:'{}'
    });
    renderCustomerTask();
  };

  const customerTaskDecision = async (actionId, decision) => {
    activeTask = await customerApi(`/api/customer/tasks/${activeTask.task_id}/actions/${actionId}/decision`, {
      method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},
      body:JSON.stringify({decision,comment:''})
    });
    renderCustomerTask();
  };

  const setConnection = online => {
    connectionOnline = online;
    const status = document.querySelector('.status-pill [data-i18n="status.progress"]');
    if (status) status.textContent = online ? t('status.progress') : t('connection.connecting');
    syncComposerState();
  };

  const connect = () => {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${protocol}//${location.host}/ws`);
    setConnection(false);
    socket.onopen = () => { setConnection(true); loadCustomerTasks().catch(() => {}); };
    socket.onmessage = event => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (_error) { return; }
      if (payload.type === 'task.snapshot' && payload.conversation_id === active) {
        tasks = payload.tasks || [];
        activeTask = tasks.find(item => !['completed','cancelled'].includes(item.status)) || tasks[0] || null;
        renderCustomerTask();
        return;
      }
      if (payload.type === 'task.event' && payload.conversation_id === active) {
        clearTimeout(socket.taskRefreshTimer);
        socket.taskRefreshTimer = setTimeout(() => loadCustomerTasks().catch(() => {}),80);
        return;
      }
      if (payload.type === 'task.selection_required') return;
      if (payload.type !== 'assistant.message' || typeof payload.content !== 'string') return;
      if (!messagesByConversation[payload.conversation_id]) return;
      messagesByConversation[payload.conversation_id].push({role:'assistant',content:payload.content});
      if (payload.conversation_id === active) renderMessages();
    };
    socket.onclose = () => { setConnection(false); clearTimeout(reconnectTimer); reconnectTimer=setTimeout(connect,3000); };
    socket.onerror = () => socket.close();
  };

  const loadServerConversations = async () => {
    const sessionResponse = await fetch('/api/customer/auth/session',{credentials:'same-origin'});
    if (sessionResponse.status === 404) {
      anonymousMode = true;
      if (!active) createAnonymousConversation();
      return false;
    }
    if (!sessionResponse.ok) return false;
    const session = await sessionResponse.json();
    csrfToken = cookieValue('nanoclaw_customer_csrf');
    if (!session.authenticated) return false;
    authenticated = true;
    anonymousMode = false;
    const payload = await customerApi('/api/customer/conversations?limit=100');
    conversations = payload.items.map(item => ({id:item.conversation_id,title:item.title,dayKey:'time.today',version:item.version}));
    messagesByConversation = Object.fromEntries(conversations.map(item => [item.id,[]]));
    active = conversations[0]?.id || '';
    if (active) await loadServerMessages(active);
    if (active) await loadCustomerTasks();
    $('conversationTitle').textContent = conversations[0]?.title || t('portal.title');
    updateAccountButton();
    renderList();
    renderMessages();
    syncComposerState();
    return true;
  };

  const setLanguage = next => {
    lang = copy[next] ? next : 'en';
    localStorage.setItem('nanoclaw-customer-language',lang);
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : lang;
    $('languageToggle').textContent = {zh:'中',en:'EN',de:'DE'}[lang];
    document.querySelectorAll('[data-i18n]').forEach(element => element.textContent=t(element.dataset.i18n));
    document.querySelectorAll('[data-i18n-placeholder]').forEach(element => element.placeholder=t(element.dataset.i18nPlaceholder));
    document.querySelectorAll('[data-i18n-aria]').forEach(element => element.setAttribute('aria-label',t(element.dataset.i18nAria)));
    updateAccountButton();setAuthMode(authMode);renderList();renderMessages();renderCustomerTask();
  };

  const customerDialog = $('customerDialog');
  const authDialog = $('customerAuthDialog');
  $('customerAccount').onclick = () => {
    setAuthMode('login');
    $('customerLogout').hidden=!authenticated;
    $('customerAuthModeToggle').hidden=authenticated;
    $('customerAuthSubmit').hidden=authenticated;
    authDialog.showModal();
  };
  $('customerAuthModeToggle').onclick = () => setAuthMode(authMode==='login'?'register':'login');
  $('customerAuthForm').onsubmit = async event => {
    event.preventDefault();
    if (event.submitter?.value === 'cancel' || event.submitter?.hasAttribute('data-auth-close')) { authDialog.close('cancel');return; }
    const registering=authMode==='register';
    const email=$('customerAuthEmail').value.trim();
    const password=$('customerAuthPassword').value;
    if (registering && password!==$('customerAuthConfirmPassword').value) {
      $('customerAuthStatus').textContent=t('auth.passwordMismatch');$('customerAuthStatus').dataset.state='error';return;
    }
    try {
      if (registering) await customerApi('/api/customer/auth/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,locale:lang})});
      await customerApi('/api/customer/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,locale:lang})});
      csrfToken=cookieValue('nanoclaw_customer_csrf');authDialog.close();await loadServerConversations();socket?.close();
    } catch (error) { $('customerAuthStatus').textContent=t(authErrorKey(error.message,registering));$('customerAuthStatus').dataset.state='error'; }
    finally { $('customerAuthPassword').value='';$('customerAuthConfirmPassword').value=''; }
  };
  $('customerLogout').onclick = async () => { await customerApi('/api/customer/auth/logout',{method:'POST'});location.reload(); };
  $('newInquiry').onclick = () => {
    if (!authenticated && !anonymousMode) { authDialog.showModal();return; }
    customerDialog.showModal();
  };
  customerDialog.addEventListener('close', async () => {
    if (customerDialog.returnValue !== 'confirm') return;
    if (anonymousMode) { createAnonymousConversation();$('inquiryInput').focus();return; }
    if (!authenticated) return;
    const created = await customerApi('/api/customer/conversations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:t('chat.newTitle')})});
    conversations.unshift({id:created.conversation_id,title:created.title,dayKey:'time.today',version:created.version});
    messagesByConversation[created.conversation_id]=[];active=created.conversation_id;
    $('conversationTitle').textContent=created.title;renderList();renderMessages();await loadCustomerTasks();syncComposerState();$('inquiryInput').focus();
  });
  $('deleteConversation').onclick = async () => {
    const current=conversations.find(item => item.id === active);if (!current) return;
    if (anonymousMode) {
      conversations=conversations.filter(item => item.id !== active);delete messagesByConversation[active];active=conversations[0]?.id||'';
      if (!active) createAnonymousConversation();
      $('conversationTitle').textContent=conversations.find(item=>item.id===active)?.title||t('portal.title');renderList();renderMessages();syncComposerState();
      return;
    }
    if (!authenticated) return;
    await customerApi(`/api/customer/conversations/${encodeURIComponent(active)}?version=${current.version}`,{method:'DELETE',headers:{'Idempotency-Key':crypto.randomUUID()}});
    conversations=conversations.filter(item => item.id !== active);delete messagesByConversation[active];active=conversations[0]?.id||'';
    $('conversationTitle').textContent=conversations[0]?.title||t('portal.title');renderList();renderMessages();await loadCustomerTasks();syncComposerState();
  };
  $('inquiryForm').onsubmit = event => {
    event.preventDefault();const value=$('inquiryInput').value.trim();if (!value || !active || socket?.readyState !== WebSocket.OPEN) return;
    messagesByConversation[active].push({role:'user',content:value});renderMessages();
    socket.send(JSON.stringify({type:'chat.message',protocol_version:2,conversation_id:active,request_id:crypto.randomUUID(),language: lang,content:value,task_id:activeTask?.task_id||undefined}));
    $('inquiryInput').value='';
  };
  $('customerTaskInstruction').onsubmit = async event => {
    event.preventDefault();const content=$('customerTaskInstructionText').value.trim();if (!content || !activeTask) return;
    activeTask=await customerApi(`/api/customer/tasks/${activeTask.task_id}/instructions`,{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({content,changes:{}})});
    $('customerTaskInstructionText').value='';renderCustomerTask();
  };
  $('conversationSearch').addEventListener('input',renderList);
  document.querySelectorAll('[data-prompt-key]').forEach(button => button.onclick=()=>{$('inquiryInput').value=t(button.dataset.promptKey);$('inquiryInput').focus();});
  $('languageToggle').onclick=()=>$('languageMenu').classList.toggle('open');
  document.querySelectorAll('[data-language]').forEach(button => button.onclick=()=>{$('languageMenu').classList.remove('open');setLanguage(button.dataset.language);});
  window.addEventListener('storage',event=>{if(event.key==='nanoclaw-customer-language'&&copy[event.newValue])setLanguage(event.newValue);});
  window.addEventListener('nanoclaw:customer-language-change',event=>{if(copy[event.detail?.language])setLanguage(event.detail.language);});

  setLanguage(lang);
  loadServerConversations().catch(() => false).finally(connect);
})();
