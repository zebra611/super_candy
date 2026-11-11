// UI 交互冒烟：5 态状态机关键路径 + 档案浏览（按类目挑全部示例数据）。
// 测的不是样式，是"操作逻辑"：idle 输入禁用/暂停隐藏；浏览列表加载与
// 类目筛选；选中档案后目标牌与档案卡出现；对话/挂断/事件链快照。
// 跑法：python app.py 8899 后 node scripts/ui_smoke.mjs
// （需要 playwright；浏览器用系统 Edge/Chrome，见 launch 的 channel。）
import { chromium } from 'playwright';
const BASE = 'http://127.0.0.1:8899';
const results = [];
const check = (name, ok, detail='') => { results.push({name, ok, detail}); };
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage();
page.on('pageerror', e => results.push({name:'页面 JS 报错', ok:false, detail:String(e)}));
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(500);

check('idle: 四个入口可用', await page.isEnabled('#btnText') && await page.isEnabled('#btnStart') && await page.isEnabled('#btnRandom') && await page.isEnabled('#btnBrowse'));
check('idle: 输入框禁用', await page.isDisabled('#textInput'));
check('idle: 暂停键隐藏', await page.isHidden('#btnRec'));

// 档案浏览
await page.click('#btnBrowse');
await page.waitForSelector('#browseBox:not([hidden])');
await page.waitForFunction(() => document.querySelectorAll('#browseList .browse-row').length > 3, null, { timeout: 6000 });
const nAll = await page.locator('#browseList .browse-row').count();
check('浏览: 列表加载', nAll > 10, 'rows=' + nAll);
// 类目筛选
const chip = page.locator('#browseChips button').nth(2);
const catName = await chip.textContent();
await chip.click();
await page.waitForTimeout(200);
const nCat = await page.locator('#browseList .browse-row').count();
check('浏览: 类目筛选生效', nCat > 0 && nCat <= nAll, catName + ' rows=' + nCat);
// 选一家 → 档案卡出现 + 目标牌 + 开始对话
await page.locator('#browseList .browse-row').first().click();
await page.waitForFunction(() => { const c = document.getElementById('targetChip'); return c && !c.hidden; }, null, { timeout: 5000 });
check('浏览: 选中后目标牌出现', await page.isVisible('#targetChip'));
check('浏览: 档案卡展示', await page.isVisible('#randomCard'));
const chipTxt = (await page.textContent('#targetChip')).trim();
check('浏览: 目标牌带公司名', chipTxt.length > 4, chipTxt);
// 收起再点随机不冲突
await page.click('#btnBrowseClose');
await page.waitForTimeout(100);
check('浏览: 收起后框隐藏', await page.isHidden('#browseBox'));
await page.click('#btnRandom');
await page.waitForTimeout(800);
check('随机: 与浏览不冲突', await page.isVisible('#randomCard'));

// 文字对话一轮
await page.click('#btnText');
await page.waitForTimeout(800);
await page.fill('#textInput', '为什么没通过');
await page.click('#btnSend');
await page.waitForTimeout(1500);
const logText = await page.textContent('#log');
check('对话: 一轮进了日志', logText.includes('为什么没通过'));
// 挂断 → 事件链快照
await page.click('#btnEnd');
await page.waitForTimeout(800);
check('挂断: 状态行=已结束', (await page.textContent('#phaseLine')).includes('已结束'));
await page.click('.tab[data-pane="eventsPane"]');
await page.waitForTimeout(300);
await page.click('#btnEvents');
await page.waitForTimeout(800);
const ev = await page.textContent('#events');
check('挂断: 事件链可读', ev.length > 40, ev.slice(0, 40));
await browser.close();
let fail = 0;
for (const r of results) { console.log((r.ok ? '  ✓ ' : '  ✗ ') + r.name + (r.ok ? '' : '  [' + r.detail + ']')); if (!r.ok) fail++; }
console.log(fail === 0 ? 'ALL PASS ' + results.length : 'FAILED ' + fail + '/' + results.length);
process.exit(fail === 0 ? 0 : 1);
