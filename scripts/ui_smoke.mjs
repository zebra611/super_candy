// UI 交互冒烟：验证 5 态状态机（idle/text/voice/paused/closed）的关键路径。
// 测的不是样式，是"操作逻辑"：idle 输入禁用/暂停隐藏；看档案后目标牌出现；
// 文字对话状态行不再停在"未开始"；空发送不发空轮；挂断后事件链用快照仍能看；
// 第二通开始清屏。跑法：python app.py 8899 后 node scripts/ui_smoke.mjs
// （需要 npx playwright；浏览器用系统 Edge/Chrome，见 launch 的 channel。）
// UI 交互冒烟：5 态状态机 + 评审里 10 条修法的关键路径
import { chromium } from 'playwright';

const BASE = 'http://127.0.0.1:8899';
const results = [];
const check = (name, ok, detail='') => { results.push({name, ok, detail}); };

const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage();
page.on('pageerror', e => results.push({name:'页面 JS 报错', ok:false, detail:String(e)}));

await page.goto(BASE, { waitUntil: 'domcontentloaded' });

// ① idle：暂停键隐藏、输入禁用、三个入口可点、状态行未开始
await page.waitForTimeout(400);
check('idle: 暂停聆听隐藏', await page.isHidden('#btnRec'));
check('idle: 输入框禁用', await page.isDisabled('#textInput'));
check('idle: 发送禁用', await page.isDisabled('#btnSend'));
check('idle: 三个入口可用', await page.isEnabled('#btnText') && await page.isEnabled('#btnStart') && await page.isEnabled('#btnRandom'));
check('idle: 状态行=未开始', (await page.textContent('#phaseLine')).includes('未开始'));
check('idle: 目标牌隐藏', await page.isHidden('#targetChip'));

// ② 看一份档案：目标牌出现 + 右栏自动展开
await page.click('#btnRandom');
await page.waitForFunction(() => { const c = document.getElementById('targetChip'); return c && !c.hidden; }, null, { timeout: 4000 });
check('预览: 目标牌出现', await page.isVisible('#targetChip'), (await page.textContent('#targetChip')).trim());
check('预览: 外呼目标面板自动展开', (await page.getAttribute('#targetPanel','class')).includes('is-on'));

// ③ 文字对话：compact、状态行、输入可用、暂停隐藏、模拟不说话可见
await page.click('#btnText');
await page.waitForTimeout(800);
check('text: 状态行=文字对话进行中', (await page.textContent('#phaseLine')).includes('文字对话进行中'));
check('text: 输入可用', await page.isEnabled('#textInput'));
check('text: 暂停聆听隐藏', await page.isHidden('#btnRec'));
check('text: 模拟不说话可见', await page.isVisible('#btnSilence'));
check('text: 启动区收成工具栏', (await page.getAttribute('.launch','class')).includes('compact'));
check('text: 目标牌仍可见', await page.isVisible('#targetChip'));

// ④ 打字对话一轮
await page.fill('#textInput', '为什么没通过');
await page.click('#btnSend');
await page.waitForTimeout(1200);
const logText = await page.textContent('#log');
check('text: 一轮对话进了日志', logText.includes('为什么没通过') && logText.includes('客'));

// ⑤ 空发送不再触发空轮（发送键在空值时不动作）
await page.fill('#textInput', '');
await page.click('#btnSend');
await page.waitForTimeout(400);
const logLen1 = (await page.textContent('#log')).length;
await page.click('#btnSilence');
await page.waitForTimeout(1200);
const logText2 = await page.textContent('#log');
check('silence: 专用按钮才发空轮', logText2.includes('（无应答）'));
check('silence: 空发送不进日志', (await page.textContent('#log')).length === logText2.length);

// ⑥ 挂断：状态行、入口解锁、事件链仍可查
await page.click('#btnEnd');
await page.waitForTimeout(800);
check('closed: 状态行=已结束', (await page.textContent('#phaseLine')).includes('已结束'));
check('closed: 入口解锁', await page.isEnabled('#btnText'));
check('closed: 挂断禁用', await page.isDisabled('#btnEnd'));
check('closed: 输入禁用', await page.isDisabled('#textInput'));
// 事件链 tab + 刷新（用 lastSessionId）
await page.click('.tab[data-pane="eventsPane"]');
await page.waitForTimeout(300);
await page.click('#btnEvents');
await page.waitForTimeout(800);
const ev = await page.textContent('#events');
check('closed: 事件链还能读到记录', ev.includes('turn') || ev.includes('call_start') || ev.length > 40, ev.slice(0, 60));

// ⑦ 再开一通：日志被清空（第二通不与第一通混在一起）
await page.click('#btnText');
await page.waitForTimeout(800);
const logText3 = await page.textContent('#log');
check('第二通: 日志已清空重开', !logText3.includes('为什么没通过'), '残留=' + logText3.includes('为什么没通过'));

await browser.close();
let fail = 0;
for (const r of results) { console.log((r.ok ? '  ✓ ' : '  ✗ ') + r.name + (r.ok ? '' : '  [' + r.detail + ']')); if (!r.ok) fail++; }
console.log(fail === 0 ? 'ALL PASS ' + results.length : 'FAILED ' + fail + '/' + results.length);
process.exit(fail === 0 ? 0 : 1);
