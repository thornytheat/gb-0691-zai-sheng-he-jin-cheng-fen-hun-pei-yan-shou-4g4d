(function () {
  const feedback = document.querySelector('#import-feedback');
  const busy = { value: false };

  async function request(url, options) {
    const response = await fetch(url, options);
    let data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok) throw new Error((data && data.message) || '请求失败');
    return data;
  }

  async function post(url, payload, isForm) {
    const options = { method: 'POST' };
    if (isForm) {
      options.body = payload;
    } else {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(payload);
    }
    return request(url, options);
  }

  function say(text, bad) {
    if (!feedback) return;
    feedback.textContent = text;
    feedback.className = 'alert' + (bad ? '' : ' muted');
  }

  async function run(action) {
    if (busy.value) return;
    busy.value = true;
    document.querySelectorAll('button').forEach((button) => { button.disabled = true; });
    try {
      await action();
    } catch (error) {
      say(error.message, true);
    } finally {
      busy.value = false;
      document.querySelectorAll('button').forEach((button) => { button.disabled = false; });
    }
  }

  const submit = document.querySelector('#ledger-submit');
  if (submit) {
    submit.addEventListener('click', () => run(async () => {
      say('正在导入台账…');
      const result = await post('/api/ledger/ingest', { csv: document.querySelector('#ledger-csv').value });
      say('导入完成：新增 ' + result.ingest.inserted + ' 条，重复未计 ' + result.ingest.duplicates +
          ' 条，隔离 ' + result.ingest.quarantined + ' 条。正在刷新结算…');
      location.href = '/ledger';
    }));
  }

  const late = document.querySelector('#ledger-late');
  if (late) {
    late.addEventListener('click', () => run(async () => {
      say('正在导入晚到记录…');
      const result = await post('/api/ledger/ingest', { csv: document.querySelector('#ledger-late-csv').value });
      say('晚到记录已入账：新增 ' + result.ingest.inserted + ' 条，重复未计 ' + result.ingest.duplicates + ' 条。');
      location.href = '/ledger';
    }));
  }

  const upload = document.querySelector('#ledger-upload');
  if (upload) {
    upload.addEventListener('click', () => run(async () => {
      const file = document.querySelector('#ledger-file').files[0];
      if (!file) { say('先选择台账 CSV 文件。', true); return; }
      const form = new FormData();
      form.append('file', file);
      say('正在上传台账…');
      await post('/api/ledger/ingest', form, true);
      location.href = '/ledger';
    }));
  }

  document.querySelectorAll('[data-issue]').forEach((button) => {
    button.addEventListener('click', () => run(async () => {
      const heat = button.dataset.issue;
      say('正在为 ' + heat + ' 出具证明…');
      const result = await post('/api/ledger/certificate', { heat: heat });
      if (result.issued) {
        say('已出具 ' + result.certificate.certificate_id + '。');
        location.href = '/ledger/certificates?heat=' + encodeURIComponent(heat);
      } else {
        say('该结算版本已有证明 ' + result.certificate.certificate_id + '，未重复生成。');
      }
    }));
  });
})();
