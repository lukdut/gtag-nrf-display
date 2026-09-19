import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';
import {IntlMessageFormat} from 'intl-messageformat';
import {marked} from 'marked';
import {filterXSS} from 'xss';

const component = new URL('../../custom_components/gtag_ble_test/', import.meta.url);

function* strings(object, prefix = '') {
  for (const [key, value] of Object.entries(object)) {
    const path = `${prefix}.${key}`;
    if (typeof value === 'object') yield* strings(value, path);
    else yield [path, value];
  }
}

for (const file of ['strings.json', 'translations/en.json', 'translations/ru.json']) {
  const language = file.includes('/ru.') ? 'ru' : 'en';
  const data = JSON.parse(readFileSync(new URL(file, component)));
  test(`${file}: all messages parse with HA's ICU formatter`, () => {
    for (const [key, text] of strings(data)) {
      assert.doesNotThrow(() => new IntlMessageFormat(text, language), key);
    }
  });

  const values = {
    filename: 'gtag-kitchen.yaml', firmware_tag: 'v0.9.0',
    download_url: '/api/gtag_ble_test/firmware/test-flow?authSig=test.signature',
    download_link_start: '<a href="/api/gtag_ble_test/firmware/test-flow?authSig=test.signature" target="_blank">',
    download_link_end: '</a>',
    yaml: 'substitutions:\n  gtag_friendly_name: "Кухня {1} <test>"\n',
    json: '{"name":"Макет {1}","screen":{"preset":"clock"}}',
  };
  test(`${file}: diagnostics renders every value through ICU and Markdown`, () => {
    const labels = data.selector.diagnostic_value.options;
    const message = new IntlMessageFormat(data.options.step.diagnostics.description, language).format({
      transport: 'Zigbee2MQTT', firmware: '0.9.0', features: 'battery_protection',
      codecs: 'white_rle_v1', last_update: '2026-09-19 12:00:00 MSK',
      last_confirmation: 'unknown', display_status: 'sent',
      display_error: 'none', stale: 'stale', check_status: 'error',
      checked_at: '2026-09-19 12:01:00 MSK', duration: '32.45',
      check_error: 'timeout', check_detail: 'No reply {request_id}: &lt;packet&gt;', battery: '4.190 V',
    });
    const html = filterXSS(marked.parse(message, {gfm: true, breaks: true}));
    assert.ok(html.includes('0.9.0') && html.includes(labels.timeout));
    assert.ok(html.includes(labels.sent) && html.includes(labels.battery_protection));
    assert.ok(html.includes('No reply {request_id}: &lt;packet&gt;'));
    assert.ok(!html.includes('INVALID_TAG'));
  });
  const descriptions = {
    download: data.config.step.firmware_download.description,
    finished: data.config.abort.firmware_ready,
    layout_export: data.options.step.export_download.description,
    layout_exported: data.options.abort.layout_exported,
  };
  for (const [step, description] of Object.entries(descriptions)) {
    test(`${file}: ${step} formats before Markdown and retains the download target`, () => {
      // HA calls IntlMessageFormat with its default tag parsing, then Markdown
      // and the xss filter. Substituted HTML/YAML must not become ICU syntax.
      const message = new IntlMessageFormat(description, language).format(values);
      assert.equal(typeof message, 'string');
      const html = filterXSS(marked.parse(message, {gfm: true, breaks: true}));
      const label = language === 'ru' ? 'Скачать' : 'Download';
      assert.ok(html.includes(`${values.download_link_start}${label} ${values.filename}</a>`), html);
      if (step === 'download') assert.ok(message.includes(values.yaml));
      if (step === 'layout_export') assert.ok(message.includes(values.json));
    });
  }
}
