const NATIVE_HOST = 'com.cc.quota_writer';
const ALARM_NAME = 'cc-quota-fetch';
const PERIOD_MINUTES = 0.5;

const fetchJson = async (url) => {
  const resp = await fetch(url, {
    credentials: 'include',
    headers: { 'anthropic-client-platform': 'web_claude_ai' }
  });
  return { status: resp.status, body: resp.ok ? await resp.json() : null };
};

const ts = () => new Date().toTimeString().slice(0, 8);

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

const fetchQuotaForOrg = async (email, orgId) => {
  try {
    const { status, body } = await fetchJson(
      `https://claude.ai/api/organizations/${orgId}/usage`
    );
    if (status === 200 && body) {
      return { type: 'quota', email, org_id: orgId, fetched_at: ts(), body };
    }
    const reason = status === 401 ? 'auth fail'
      : status === 403 ? 'forbidden'
      : status === 429 ? 'rate limited'
      : `HTTP ${status}`;
    return { type: 'status', email, org_id: orgId, fetched_at: ts(), reason };
  } catch (e) {
    return { type: 'status', email, org_id: orgId, fetched_at: ts(), reason: 'network' };
  }
};

const tick = async () => {
  let bootstrap;
  try {
    const r = await fetchJson('https://claude.ai/api/bootstrap');
    bootstrap = r.body;
  } catch (e) {
    await send({ type: 'status', email: 'unknown', fetched_at: ts(), reason: 'bootstrap network' });
    return;
  }
  const email = bootstrap?.account?.email_address;
  const memberships = bootstrap?.account?.memberships ?? [];
  if (!email) {
    await send({ type: 'status', email: 'unknown', fetched_at: ts(), reason: 'not logged in' });
    return;
  }
  const primaryOrg = memberships.find((m) => m.organization?.organization_type === 'team')
    ?? memberships.find((m) => !!m.organization?.uuid)
    ?? null;
  const orgId = primaryOrg?.organization?.uuid;
  if (!orgId) {
    await send({ type: 'status', email, fetched_at: ts(), reason: 'no org' });
    return;
  }
  const result = await fetchQuotaForOrg(email, orgId);
  await send(result);
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
