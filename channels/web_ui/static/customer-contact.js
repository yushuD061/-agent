(() => {
  'use strict';

  const copy = {
    zh: {
      contactNav: '联系我们', contactEyebrow: 'CONTACT US', contactTitle: '联系我们',
      contactDescription: '如果您有产品、询价或订单方面的问题，欢迎通过电子邮件联系我们。',
      emailLabel: '公司联系邮箱', ratesNav: '汇率', ratesEyebrow: 'EXCHANGE RATES', ratesTitle: '参考汇率',
      ratesDescription: '查询常用贸易货币的参考换算结果。最终合同结算汇率请与我们的销售团队确认。',
      amount: '金额', from: '从', to: '到', waiting: '正在获取最新参考汇率…', loading: '正在加载…',
      refresh: '刷新', disclaimer: '汇率数据仅供询价参考，不构成报价或合同承诺。',
      updated: '更新于', failed: '暂时无法获取汇率，请稍后重试。', swap: '交换币种'
    },
    en: {
      contactNav: 'Contact us', contactEyebrow: 'CONTACT US', contactTitle: 'Contact us',
      contactDescription: 'For product, quotation, or order inquiries, please contact us by email.',
      emailLabel: 'Company email', ratesNav: 'Exchange rates', ratesEyebrow: 'EXCHANGE RATES', ratesTitle: 'Reference exchange rates',
      ratesDescription: 'Convert common trade currencies for reference. Please confirm the final contractual rate with our sales team.',
      amount: 'Amount', from: 'From', to: 'To', waiting: 'Fetching the latest reference rates…', loading: 'Loading…',
      refresh: 'Refresh', disclaimer: 'Rates are for inquiry reference only and do not constitute a quotation or contractual commitment.',
      updated: 'Updated', failed: 'Exchange rates are temporarily unavailable. Please try again later.', swap: 'Swap currencies'
    },
    de: {
      contactNav: 'Kontakt', contactEyebrow: 'KONTAKT', contactTitle: 'Kontaktieren Sie uns',
      contactDescription: 'Bei Fragen zu Produkten, Angeboten oder Bestellungen kontaktieren Sie uns bitte per E-Mail.',
      emailLabel: 'Firmen-E-Mail', ratesNav: 'Wechselkurse', ratesEyebrow: 'WECHSELKURSE', ratesTitle: 'Referenzwechselkurse',
      ratesDescription: 'Rechnen Sie gängige Handelswährungen unverbindlich um. Den endgültigen Vertragskurs bestätigen Sie bitte mit unserem Vertrieb.',
      amount: 'Betrag', from: 'Von', to: 'Nach', waiting: 'Aktuelle Referenzkurse werden geladen…', loading: 'Laden…',
      refresh: 'Aktualisieren', disclaimer: 'Die Kurse dienen nur als Anfrage-Referenz und sind kein Angebot oder Vertragsversprechen.',
      updated: 'Aktualisiert', failed: 'Wechselkurse sind vorübergehend nicht verfügbar. Bitte versuchen Sie es später erneut.', swap: 'Währungen tauschen'
    }
  };
  const currencyNames = {
    USD: 'US Dollar', EUR: 'Euro', CNY: 'Chinese Yuan', GBP: 'British Pound', JPY: 'Japanese Yen',
    CAD: 'Canadian Dollar', AUD: 'Australian Dollar', CHF: 'Swiss Franc', HKD: 'Hong Kong Dollar', SGD: 'Singapore Dollar'
  };

  window.addEventListener('DOMContentLoaded', () => {
    const shell = document.querySelector('.customer-shell');
    const sidebar = document.querySelector('.customer-sidebar');
    const conversationPage = document.querySelector('#conversationPage');
    const ratesPage = document.querySelector('#ratesPage');
    const contactPage = document.querySelector('#contactPage');
    const conversationNav = document.querySelector('[data-section="conversations"]');
    const ratesNav = document.querySelector('[data-section="rates"]');
    const contactNav = document.querySelector('[data-section="contact"]');
    if (!shell || !sidebar || !conversationPage || !ratesPage || !contactPage
        || !conversationNav || !ratesNav || !contactNav) return;

    let rates = null;
    let ratesUpdatedAt = null;
    const language = () => localStorage.getItem('nanoclaw-customer-language') || 'en';
    const currentCopy = () => copy[language()] || copy.en;

    const updateCopy = () => {
      const current = currentCopy();
      contactNav.querySelector('[data-contact-label]').textContent = current.contactNav;
      ratesNav.querySelector('[data-rates-label]').textContent = current.ratesNav;
      contactNav.setAttribute('aria-label', current.contactNav);
      ratesNav.setAttribute('aria-label', current.ratesNav);
      const contactKeys = { eyebrow: 'contactEyebrow', title: 'contactTitle', description: 'contactDescription', emailLabel: 'emailLabel' };
      contactPage.querySelectorAll('[data-contact-copy]').forEach(element => {
        element.textContent = current[contactKeys[element.dataset.contactCopy]];
      });
      const rateKeys = { eyebrow: 'ratesEyebrow', title: 'ratesTitle', description: 'ratesDescription' };
      ratesPage.querySelectorAll('[data-rates-copy]').forEach(element => {
        const key = rateKeys[element.dataset.ratesCopy] || element.dataset.ratesCopy;
        if (current[key] !== undefined) element.textContent = current[key];
      });
      document.querySelector('#customerRateSwap').setAttribute('aria-label', current.swap);
      renderRate();
    };

    const loadContactEmail = async () => {
      try {
        const response = await fetch('/api/public/config', { cache: 'no-store' });
        const config = await response.json();
        if (!response.ok || typeof config.sales_email !== 'string' || !config.sales_email.includes('@')) return;
        const link = contactPage.querySelector('.contact-email-block a');
        link.textContent = config.sales_email;
        link.href = `mailto:${config.sales_email}`;
      } catch (_error) {
        // Keep the fallback already rendered in the page.
      }
    };

    const formatMoney = (value, currency) => new Intl.NumberFormat(language(), {
      style: 'currency', currency, maximumFractionDigits: currency === 'JPY' ? 0 : 2
    }).format(value);
    const renderRate = () => {
      if (!rates) return;
      const amount = Number(document.querySelector('#customerRateAmount').value);
      const from = document.querySelector('#customerRateFrom').value;
      const to = document.querySelector('#customerRateTo').value;
      if (!Number.isFinite(amount) || amount < 0) return;
      const cross = rates[to] / rates[from];
      document.querySelector('#customerRateEquation').textContent = `${formatMoney(amount, from)} =`;
      document.querySelector('#customerRateValue').textContent = formatMoney(amount * cross, to);
      document.querySelector('#customerRateDetail').textContent = `1 ${from} = ${new Intl.NumberFormat(language(), { maximumFractionDigits: 6 }).format(cross)} ${to}`;
      if (ratesUpdatedAt) {
        document.querySelector('#customerRateTimestamp').textContent = `${currentCopy().updated}: ${ratesUpdatedAt.toLocaleString(language())}`;
      }
    };
    const loadRates = async () => {
      document.querySelector('#customerRateTimestamp').textContent = currentCopy().loading;
      try {
        const response = await fetch('https://open.er-api.com/v6/latest/USD', { cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || data.result !== 'success' || !data.rates) throw new Error('invalid_rate_response');
        rates = data.rates;
        ratesUpdatedAt = new Date((data.time_last_update_unix || Date.now() / 1000) * 1000);
        renderRate();
      } catch (_error) {
        document.querySelector('#customerRateTimestamp').textContent = currentCopy().failed;
        document.querySelector('#customerRateDetail').textContent = currentCopy().failed;
      }
    };

    const showPage = page => {
      const showConversation = page === 'conversations';
      const showRates = page === 'rates';
      const showContact = page === 'contact';
      shell.classList.toggle('secondary-active', !showConversation);
      conversationPage.hidden = !showConversation;
      ratesPage.hidden = !showRates;
      contactPage.hidden = !showContact;
      sidebar.setAttribute('aria-hidden', String(!showConversation));
      [[conversationNav, showConversation], [ratesNav, showRates], [contactNav, showContact]].forEach(([nav, active]) => {
        nav.classList.toggle('active', active);
        nav.toggleAttribute('aria-current', active);
      });
      if (showRates && !rates) loadRates();
      if (showContact) loadContactEmail();
    };

    ['customerRateFrom', 'customerRateTo'].forEach(id => {
      const select = document.querySelector(`#${id}`);
      Object.entries(currencyNames).forEach(([code, name]) => select.add(new Option(`${code} · ${name}`, code)));
    });
    document.querySelector('#customerRateFrom').value = 'USD';
    document.querySelector('#customerRateTo').value = 'CNY';
    ['customerRateAmount', 'customerRateFrom', 'customerRateTo'].forEach(id => {
      document.querySelector(`#${id}`).addEventListener(id === 'customerRateAmount' ? 'input' : 'change', renderRate);
    });
    document.querySelector('#customerRateSwap').addEventListener('click', () => {
      const from = document.querySelector('#customerRateFrom');
      const to = document.querySelector('#customerRateTo');
      [from.value, to.value] = [to.value, from.value];
      renderRate();
    });
    document.querySelector('#customerRateRefresh').addEventListener('click', loadRates);
    conversationNav.addEventListener('click', () => showPage('conversations'));
    ratesNav.addEventListener('click', () => showPage('rates'));
    contactNav.addEventListener('click', () => showPage('contact'));
    document.querySelectorAll('[data-language]').forEach(button => {
      button.addEventListener('click', () => queueMicrotask(updateCopy));
    });
    updateCopy();
    loadContactEmail();
    window.addEventListener('focus', loadContactEmail);
    showPage('conversations');
  });
})();
