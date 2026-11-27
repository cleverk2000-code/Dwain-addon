const {test, expect} = require('@playwright/test');
const path = require('node:path');
const source = path.join(__dirname, '../custom_components/dwain_addon/manager.js');

test('local panel preview, apply, invalidation, cancellation and recovery', async ({page}) => {
  const calls = [];
  await page.route('**/*', route => route.abort());
  await page.setContent('<dwain-addon-manager></dwain-addon-manager>');
  await page.addScriptTag({path:source});
  await page.evaluate(() => {
    window.requests = [];
    window.snap = {revision:'abc123',registry:{areas:{living:{name:'<script>bad</script>'}},entities:{}},configuration:{areas:{},entities:{}},backups:['backup1'],pending:null};
    document.querySelector('dwain-addon-manager').hass = {callApi: async (method, route, body) => {
      window.requests.push({method,route,body});
      if (method === 'GET') return structuredClone(window.snap);
      if (body.action === 'preview' || body.action === 'restore_preview') return {revision:'abc123',changes:[]};
      if (body.action === 'recover') {window.snap.pending=null;return {recovered:true};}
      return {changed:true,backup_id:'backup2'};
    }};
  });
  const el = id => page.locator('dwain-addon-manager').locator('#'+id);
  await expect(el('status')).toContainText('설정 조회 완료');
  await expect(el('apply')).toBeDisabled();
  await el('patch').fill('{"areas":{"living":{"sort_order":1}}}');
  await el('preview').click();
  await expect(el('apply')).toBeEnabled();
  await el('patch').fill('{"areas":{"living":{"sort_order":2}}}');
  await expect(el('apply')).toBeDisabled();
  await el('preview').click();
  page.once('dialog', d=>d.dismiss());
  await el('apply').click();
  await expect(el('apply')).toBeEnabled();
  page.once('dialog', d=>d.accept());
  await el('apply').click();
  await expect(el('status')).toContainText('자동 백업');
  let req = await page.evaluate(()=>window.requests);
  expect(req.filter(r=>r.body?.action==='apply')).toHaveLength(1);
  expect(req.find(r=>r.body?.action==='apply').body.patch.areas.living.sort_order).toBe(2);
  await el('restorePreview').click();
  await expect(el('restore')).toBeEnabled();
  page.once('dialog',d=>d.accept());
  await el('restore').click();
  await expect(el('status')).toContainText('자동 백업');
  await page.evaluate(()=>window.snap.pending='pending1');
  await el('reload').click();
  await expect(el('recover')).toBeEnabled();
  await expect(el('preview')).toBeDisabled();
  page.once('dialog',d=>d.accept());
  await el('recover').click();
  await expect(el('status')).toContainText('복구 완료');
  await expect(el('recover')).toBeDisabled();
  expect(await page.locator('dwain-addon-manager').locator('script').count()).toBe(0);
});

test('failed preview and in-flight duplicate click are safe', async ({page}) => {
  await page.route('**/*', route => route.abort());
  await page.setContent('<dwain-addon-manager></dwain-addon-manager>');
  await page.addScriptTag({path:source});
  await page.evaluate(() => {
    window.count=0;
    document.querySelector('dwain-addon-manager').hass={callApi:async(method,route,body)=>{
      if(method==='GET') return {revision:'abc',backups:[],pending:null};
      window.count++;
      return new Promise((resolve,reject)=>window.release=()=>reject(new Error('stale revision')));
    }};
  });
  const el=id=>page.locator('dwain-addon-manager').locator('#'+id);
  await expect(el('preview')).toBeEnabled();
  await el('preview').click();
  await expect(el('preview')).toBeDisabled();
  await expect(el('apply')).toBeDisabled();
  await page.evaluate(()=>window.release());
  await expect(el('status')).toContainText('stale revision');
  expect(await page.evaluate(()=>window.count)).toBe(1);
  await expect(el('apply')).toBeDisabled();
});
