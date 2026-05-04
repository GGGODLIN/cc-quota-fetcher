const KEY = 'vendor_enabled';
const DEFAULT = { deepseek: true, mimo: true };

const load = async () => {
  const stored = await chrome.storage.local.get(KEY);
  return Object.assign({}, DEFAULT, stored[KEY] ?? {});
};

const save = async (cfg) => {
  await chrome.storage.local.set({ [KEY]: cfg });
};

const init = async () => {
  const cfg = await load();
  for (const vendor of ['deepseek', 'mimo']) {
    const cb = document.getElementById(vendor);
    cb.checked = cfg[vendor] !== false;
    cb.addEventListener('change', async () => {
      cfg[vendor] = cb.checked;
      await save(cfg);
      if (!cb.checked) {
        chrome.runtime.sendMessage({ type: 'vendor-disable', vendor });
      }
    });
  }
  const profileId = (await chrome.storage.local.get('profile_id')).profile_id ?? '?';
  document.getElementById('status').textContent = `profile: ${profileId}`;
};

init();
