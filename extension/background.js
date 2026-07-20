const NATIVE_HOST = 'com.cc.quota_writer';
const ALARM_NAME = 'cc-quota-fetch';
const PERIOD_MINUTES = 0.5;

const MIMO_BALANCE_URL = 'https://platform.xiaomimimo.com/api/v1/balance';
const MIMO_PLAN_URL = 'https://platform.xiaomimimo.com/api/v1/tokenPlan/usage';
const GLM_QUOTA_URL = 'https://api.z.ai/api/monitor/usage/quota/limit';

const FETCH_TIMEOUT_MS = 15000;
const fetchJson = async (url, opts = {}) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const resp = await fetch(url, Object.assign({ credentials: 'include', signal: ctrl.signal }, opts));
    return { status: resp.status, body: resp.ok ? await resp.json() : null };
  } finally {
    clearTimeout(timer);
  }
};

const ts = () => new Date().toTimeString().slice(0, 8);

let _profileId = null;
let _profileIdPromise = null;
const getProfileId = async () => {
  if (_profileId) return _profileId;
  if (_profileIdPromise) return _profileIdPromise;
  _profileIdPromise = (async () => {
    const stored = await chrome.storage.local.get('profile_id');
    if (stored.profile_id) { _profileId = stored.profile_id; return _profileId; }
    _profileId = crypto.randomUUID().slice(0, 8);
    await chrome.storage.local.set({ profile_id: _profileId });
    return _profileId;
  })();
  try {
    return await _profileIdPromise;
  } finally {
    _profileIdPromise = null;
  }
};

const VENDOR_DEFAULTS = { deepseek: false, mimo: false, glm: false };
const DEEPSEEK_KEY_MIGRATION_VERSION = 1;

const fingerprintSecret = async (secret) => {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
};

const getGlmApiKey = async () => {
  const stored = await chrome.storage.local.get('glm_api_key');
  return stored.glm_api_key ?? '';
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

const migrateDeepSeekKey = async () => {
  try {
    const stored = await chrome.storage.local.get([
      'deepseek_api_key',
      'deepseek_key_migration_version',
      'deepseek_key_migration_fingerprint'
    ]);
    const secret = stored.deepseek_api_key;
    if (typeof secret !== 'string' || secret.length === 0) return;
    const fingerprint = await fingerprintSecret(secret);
    if (
      stored.deepseek_key_migration_version === DEEPSEEK_KEY_MIGRATION_VERSION
      && stored.deepseek_key_migration_fingerprint === fingerprint
    ) return;
    const pid = await getProfileId();
    const migrated = await send({
      type: 'migrate-deepseek-key-v1',
      profile_id: pid,
      secret
    });
    if (migrated?.ok !== true) return;
    const current = await chrome.storage.local.get('deepseek_api_key');
    if (current.deepseek_api_key !== secret) return;
    const cleared = await send({ type: 'vendor-clear', profile_id: pid, vendor: 'deepseek' });
    if (cleared?.ok !== true) return;
    const confirmed = await chrome.storage.local.get('deepseek_api_key');
    if (confirmed.deepseek_api_key !== secret) return;
    const latest = await chrome.storage.local.get('vendor_enabled');
    const config = Object.assign({}, VENDOR_DEFAULTS, latest.vendor_enabled ?? {}, { deepseek: false });
    await chrome.storage.local.set({
      vendor_enabled: config,
      deepseek_key_migration_version: DEEPSEEK_KEY_MIGRATION_VERSION,
      deepseek_key_migration_fingerprint: fingerprint
    });
  } catch (e) {
    return;
  }
};

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

const fetchGlmQuota = async (pid) => {
  const apiKey = await getGlmApiKey();
  if (!apiKey) return;
  try {
    const { status, body } = await fetchJson(GLM_QUOTA_URL, {
      credentials: 'omit',
      headers: {
        'Authorization': apiKey,
        'Accept-Language': 'en-US,en',
        'Content-Type': 'application/json'
      }
    });
    if (status === 200 && body?.success && body.data) {
      await send({ type: 'vendor-balance', profile_id: pid, vendor: 'glm', fetched_at: ts(), data: {
        level: body.data.level ?? 'glm',
        limits: body.data.limits ?? []
      }});
    } else {
      const reason = body?.msg ? `msg ${body.msg}` : `HTTP ${status}`;
      await send({ type: 'vendor-status', profile_id: pid, vendor: 'glm', fetched_at: ts(), reason });
    }
  } catch (e) {
    await send({ type: 'vendor-status', profile_id: pid, vendor: 'glm', fetched_at: ts(), reason: 'network' });
  }
};

const ensureAlarm = async () => {
  const existing = await chrome.alarms.get(ALARM_NAME);
  if (!existing) chrome.alarms.create(ALARM_NAME, { periodInMinutes: PERIOD_MINUTES });
};

const tick = async () => {
  await ensureAlarm();
  const pid = await getProfileId();
  await fetchAnthropicQuota(pid);
};

void migrateDeepSeekKey();

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
