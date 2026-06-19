const KEY = 'vendor_enabled';
const DEFAULT = { deepseek: true, mimo: true, glm: true };

const load = async () => {
  const stored = await chrome.storage.local.get(KEY);
  return Object.assign({}, DEFAULT, stored[KEY] ?? {});
};

const save = async (cfg) => {
  await chrome.storage.local.set({ [KEY]: cfg });
};

const init = async () => {
  const cfg = await load();
  for (const vendor of ['deepseek', 'mimo', 'glm']) {
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

  const dsKey = document.getElementById('ds-key');
  const dsStored = await chrome.storage.local.get('deepseek_api_key');
  dsKey.value = dsStored.deepseek_api_key ?? '';
  dsKey.addEventListener('change', async () => {
    await chrome.storage.local.set({ deepseek_api_key: dsKey.value.trim() });
  });

  const glmKey = document.getElementById('glm-key');
  const glmStored = await chrome.storage.local.get('glm_api_key');
  glmKey.value = glmStored.glm_api_key ?? '';
  glmKey.addEventListener('change', async () => {
    await chrome.storage.local.set({ glm_api_key: glmKey.value.trim() });
  });

  const profileId = (await chrome.storage.local.get('profile_id')).profile_id ?? '?';
  document.getElementById('status').textContent = `profile: ${profileId}`;
};

init();
