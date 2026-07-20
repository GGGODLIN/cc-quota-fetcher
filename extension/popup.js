const init = async () => {
  const profileId = (await chrome.storage.local.get('profile_id')).profile_id ?? '?';
  document.getElementById('status').textContent = `Claude active · profile: ${profileId}`;
};

void init();
