(() => {
  'use strict';
  const controlCopy = {
    zh: { railOpen: '展开菜单栏', railClose: '收起菜单栏', sidebarOpen: '展开对话栏', sidebarClose: '收起对话栏' },
    en: { railOpen: 'Expand menu', railClose: 'Collapse menu', sidebarOpen: 'Show conversations', sidebarClose: 'Hide conversations' },
    de: { railOpen: 'Menü erweitern', railClose: 'Menü einklappen', sidebarOpen: 'Unterhaltungen zeigen', sidebarClose: 'Unterhaltungen ausblenden' }
  };
  const railStorageKey = 'nanoclaw-customer-rail-expanded';
  const sidebarStorageKey = 'nanoclaw-customer-sidebar-collapsed';
  const language = () => localStorage.getItem('nanoclaw-customer-language') || 'zh';
  const updateControls = (shell, railToggle, sidebarToggle) => {
    const copy = controlCopy[language()] || controlCopy.en;
    const railExpanded = shell.classList.contains('rail-expanded');
    const sidebarCollapsed = shell.classList.contains('sidebar-collapsed');
    railToggle.textContent = railExpanded ? '«' : '»';
    railToggle.title = railExpanded ? copy.railClose : copy.railOpen;
    railToggle.setAttribute('aria-label', railToggle.title);
    railToggle.setAttribute('aria-expanded', String(railExpanded));
    sidebarToggle.textContent = sidebarCollapsed ? '»' : '«';
    sidebarToggle.title = sidebarCollapsed ? copy.sidebarOpen : copy.sidebarClose;
    sidebarToggle.setAttribute('aria-label', sidebarToggle.title);
    sidebarToggle.setAttribute('aria-expanded', String(!sidebarCollapsed));
    document.querySelectorAll('.customer-nav').forEach(button => {
      const label = button.querySelector('b')?.textContent?.trim();
      if (label) button.setAttribute('aria-label', label);
    });
  };
  window.addEventListener('DOMContentLoaded', () => {
    const shell = document.querySelector('.customer-shell');
    const rail = document.querySelector('.customer-rail');
    const sidebar = document.querySelector('.customer-sidebar');
    const header = document.querySelector('.customer-header');
    if (!shell || !rail || !sidebar || !header) return;
    shell.classList.toggle('rail-expanded', localStorage.getItem(railStorageKey) === '1');
    shell.classList.toggle('sidebar-collapsed', localStorage.getItem(sidebarStorageKey) === '1');
    const railToggle = document.createElement('button');
    railToggle.type = 'button';
    railToggle.className = 'rail-expand-toggle';
    const sidebarToggle = document.createElement('button');
    sidebarToggle.type = 'button';
    sidebarToggle.className = 'sidebar-collapse-toggle';
    rail.querySelector('.customer-brand')?.after(railToggle);
    header.prepend(sidebarToggle);
    railToggle.addEventListener('click', () => {
      shell.classList.toggle('rail-expanded');
      localStorage.setItem(railStorageKey, shell.classList.contains('rail-expanded') ? '1' : '0');
      updateControls(shell, railToggle, sidebarToggle);
    });
    sidebarToggle.addEventListener('click', () => {
      if (matchMedia('(max-width: 680px)').matches) sidebar.classList.toggle('open');
      else {
        shell.classList.toggle('sidebar-collapsed');
        localStorage.setItem(sidebarStorageKey, shell.classList.contains('sidebar-collapsed') ? '1' : '0');
      }
      updateControls(shell, railToggle, sidebarToggle);
    });
    updateControls(shell, railToggle, sidebarToggle);
    document.querySelectorAll('[data-language]').forEach(button => {
      button.addEventListener('click', () => queueMicrotask(() => {
        updateControls(shell, railToggle, sidebarToggle);
      }));
    });
  });
})();
