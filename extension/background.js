const NATIVE_HOST = 'com.cc.quota_writer';
const ALARM_NAME = 'cc-quota-fetch';
const PERIOD_MINUTES = 0.5;

const DEEPSEEK_BALANCE_URL = 'https://api.deepseek.com/user/balance';
const MIMO_BALANCE_URL = 'https://platform.xiaomimimo.com/api/v1/balance';
const MIMO_PLAN_URL = 'https://platform.xiaomimimo.com/api/v1/tokenPlan/usage';

const fetchJson = async (url, opts = {}) => {
  const resp = await fetch(url, Object.assign({ credentials: 'include' }, opts));
  return { status: resp.status, body: resp.ok ? await resp.json() : null };
};

const ts = () => new Date().toTimeString().slice(0, 8);

let _profileId = null;
const getProfileId = async () => {
  if (_profileId) return _profileId;
  const stored = await chrome.storage.local.get('profile_id');
  if (stored.profile_id) { _profileId = stored.profile_id; return _profileId; }
  _profileId = crypto.randomUUID().slice(0, 8);
  await chrome.storage.local.set({ profile_id: _profileId });
  return _profileId;
};

const VENDOR_DEFAULTS = { deepseek: true, mimo: true };

const getVendorConfig = async () => {
  const stored = await chrome.storage.local.get('vendor_enabled');
  return Object.assign({}, VENDOR_DEFAULTS, stored.vendor_enabled ?? {});
};

const getDeepSeekApiKey = async () => {
  const stored = await chrome.storage.local.get('deepseek_api_key');
  return stored.deepseek_api_key ?? '';
};

const send = (payload) => new Promise((resolve) => {
  try {
    chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (resp) => {
      if (chrome.runtime.lastError) {
        console.warn('native msg error:', chrome.runtime.lastError.message);
      }
      resolve(resp);
    });
  } catch (e) {
    console.warn('sendNativeMessage threw:', e);
    resolve(null);
  }
});

const fetchAnthropicQuota = async (pid) => {
  let bootstrap;
  try {
    const r = await fetchJson('https://claude.ai/api/bootstrap');
    bootstrap = r.body;
  } catch (e) {
    await send({ type: 'status', profile_id: pid, email: 'unknown', fetched_at: ts(), reason: 'bootstrap network' });
    return;
  }
  const email = bootstrap?.account?.email_address;
  const memberships = bootstrap?.account?.memberships ?? [];
  if (!email) {
    await send({ type: 'status', profile_id: pid, email: 'unknown', fetched_at: ts(), reason: 'not logged in' });
    return;
  }
  const primaryOrg = memberships.find((m) => m.organization?.organization_type === 'team')
    ?? memberships.find((m) => !!m.organization?.uuid)
    ?? null;
  const orgId = primaryOrg?.organization?.uuid;
  if (!orgId) {
    await send({ type: 'status', profile_id: pid, email, fetched_at: ts(), reason: 'no org' });
    return;
  }
  try {
    const { status, body } = await fetchJson(
      `https://claude.ai/api/organizations/${orgId}/usage`,
      { headers: { 'anthropic-client-platform': 'web_claude_ai' } }
    );
    if (status === 200 && body) {
      await send({ type: 'quota', profile_id: pid, email, org_id: orgId, fetched_at: ts(), body });
    } else {
      const reason = status === 401 ? 'auth fail'
        : status === 403 ? 'forbidden'
        : status === 429 ? 'rate limited'
        : `HTTP ${status}`;
      await send({ type: 'status', profile_id: pid, email, org_id: orgId, fetched_at: ts(), reason });
    }
  } catch (e) {
    await send({ type: 'status', profile_id: pid, email, org_id: orgId, fetched_at: ts(), reason: 'network' });
  }
};

const fetchDeepSeekBalance = async (pid) => {
  const apiKey = await getDeepSeekApiKey();
  if (!apiKey) return;
  try {
    const { status, body } = await fetchJson(DEEPSEEK_BALANCE_URL, {
      credentials: 'omit',
      headers: {
        'Authorization': `Bearer ${apiKey}`,
        'Accept': 'application/json'
      }
    });
    if (status === 200 && body) {
      const info = body.balance_infos?.[0] ?? {};
      await send({ type: 'vendor-balance', profile_id: pid, vendor: 'deepseek', fetched_at: ts(), data: {
        balance: info.total_balance ?? '0',
        currency: info.currency ?? 'USD'
      }});
    } else {
      await send({ type: 'vendor-status', profile_id: pid, vendor: 'deepseek', fetched_at: ts(), reason: `HTTP ${status}` });
    }
  } catch (e) {
    await send({ type: 'vendor-status', profile_id: pid, vendor: 'deepseek', fetched_at: ts(), reason: 'network' });
  }
};

const fetchMiMoBalance = async (pid) => {
  try {
    const { status, body } = await fetchJson(MIMO_BALANCE_URL, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' }
    });
    if (status === 200 && body?.code === 0) {
      await send({ type: 'vendor-balance', profile_id: pid, vendor: 'mimo', fetched_at: ts(), data: {
        balance: body.data.balance ?? '0',
        currency: body.data.currency ?? 'USD'
      }});
    } else {
      const reason = body?.code ? `code ${body.code}` : `HTTP ${status}`;
      await send({ type: 'vendor-status', profile_id: pid, vendor: 'mimo', fetched_at: ts(), reason });
    }
  } catch (e) {
    await send({ type: 'vendor-status', profile_id: pid, vendor: 'mimo', fetched_at: ts(), reason: 'network' });
  }
};

const fetchMiMoPlan = async (pid) => {
  try {
    const { status, body } = await fetchJson(MIMO_PLAN_URL, {
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' }
    });
    if (status === 200 && body?.code === 0) {
      const mu = body.data?.monthUsage ?? {};
      const planItem = (mu.items ?? []).find(i => i.name === 'month_total_token');
      await send({ type: 'vendor-plan', profile_id: pid, vendor: 'mimo', fetched_at: ts(), data: {
        month_percent: mu.percent ?? 0,
        month_used: planItem?.used ?? 0,
        month_limit: planItem?.limit ?? 0
      }});
    } else {
      const reason = body?.code ? `code ${body.code}` : `HTTP ${status}`;
      await send({ type: 'vendor-status', profile_id: pid, vendor: 'mimo', fetched_at: ts(), reason: `plan ${reason}` });
    }
  } catch (e) {
    await send({ type: 'vendor-status', profile_id: pid, vendor: 'mimo', fetched_at: ts(), reason: 'plan network' });
  }
};

const tick = async () => {
  const pid = await getProfileId();
  const cfg = await getVendorConfig();
  await fetchAnthropicQuota(pid);
  if (cfg.deepseek) await fetchDeepSeekBalance(pid);
  if (cfg.mimo) {
    await fetchMiMoBalance(pid);
    await fetchMiMoPlan(pid);
  }
};

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: PERIOD_MINUTES });
  tick();
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: PERIOD_MINUTES });
  tick();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) tick();
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'vendor-disable') {
    getProfileId().then((pid) => {
      send({ type: 'vendor-clear', profile_id: pid, vendor: msg.vendor });
    });
  }
});
