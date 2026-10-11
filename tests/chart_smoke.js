/* 离线冒烟：用最小 DOM 桩执行报告里的图表脚本，验证缩放/平移逻辑。 */
const fs = require('fs');
const path = require('path');

const htmlFile = path.join(__dirname, '..', 'output', 'report.html');
if (!fs.existsSync(htmlFile)) { console.error('缺少 output/report.html，请先运行 python run.py'); process.exit(2); }
const html = fs.readFileSync(htmlFile, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) { console.error('report.html 中未找到内联图表脚本'); process.exit(2); }
const src = m[1];

function mkClassList() {
  const s = new Set();
  return {
    add: (c) => s.add(c),
    remove: (c) => s.delete(c),
    contains: (c) => s.has(c),
    toggle: (c, on) => (on ? s.add(c) : s.delete(c)),
  };
}

function mkEl(id) {
  const el = {
    id,
    _attr: {},
    style: {},
    innerHTML: '',
    textContent: '',
    _handlers: {},
    classList: mkClassList(),
    addEventListener(t, f) { (el._handlers[t] = el._handlers[t] || []).push(f); },
    setAttribute(k, v) { el._attr[k] = String(v); },
    getAttribute(k) { return k in el._attr ? el._attr[k] : null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 940, height: 330 }; },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
  return el;
}

const els = {};
const get = (id) => (els[id] = els[id] || mkEl(id));

// 工具栏按钮
const buttons = [
  { act: 'range', days: '21', label: '近1月' },
  { act: 'range', days: '63', label: '近3月' },
  { act: 'range', days: '244', label: '近1年' },
  { act: 'all', label: '全部' },
  { act: 'pan', dir: '-1', label: '‹' },
  { act: 'pan', dir: '1', label: '›' },
].map((b) => {
  const el = mkEl('btn-' + b.label);
  el.getAttribute = (k) => (k === 'data-act' ? b.act : k === 'data-days' ? b.days : k === 'data-dir' ? b.dir : null);
  el.closest = () => el;
  return el;
});
const bar = get('afgiToolbar');
bar.querySelectorAll = () => buttons;

const doc = { getElementById: (id) => get(id) };
const win = { _handlers: {}, addEventListener(t, f) { (win._handlers[t] = win._handlers[t] || []).push(f); } };

const ctx = { document: doc, window: win, isNaN, parseFloat, parseInt, Math, Date, Array, JSON };
const fn = new Function('document', 'window', src);
fn.call(ctx, doc, win);

const svgHost = get('afgiChart');
const status = get('afgiRange');
const svg = get('afgiSvg');
const fire = (el, type, evt) => (el._handlers[type] || []).forEach((f) => f(evt));
const fireWin = (type, evt) => (win._handlers[type] || []).forEach((f) => f(evt));

let failed = 0;
function check(name, cond, extra) {
  console.log((cond ? '  OK   ' : '  FAIL ') + name + (extra ? '  ' + extra : ''));
  if (!cond) failed++;
}

// 1) 初始渲染：默认区间应为「近1年」244 个交易日
const btn1M = buttons[0], btn1Y = buttons[2], btnAll = buttons[3];
check('初始渲染出 SVG', svgHost.innerHTML.startsWith('<svg'), 'len=' + svgHost.innerHTML.length);
check('默认区间为近1年（244 日）', /（244 个交易日）/.test(status.textContent), status.textContent);
check('默认高亮「近1年」', btn1Y.classList.contains('active'), '近1年 active=' + btn1Y.classList.contains('active'));
check('默认不高亮「全部」', !btnAll.classList.contains('active'));
check('AFGI 折线路径存在', svgHost.innerHTML.includes('stroke="#c0392b"'));

// 2) 预设区间按钮（含高亮同步）
fire(bar, 'click', { target: btn1M });
check('点击「近1月」后区间为 21 日', /（21 个交易日）/.test(status.textContent), status.textContent);
check('点击后高亮转移到「近1月」', btn1M.classList.contains('active') && !btn1Y.classList.contains('active'));
fire(bar, 'click', { target: btn1Y });
check('点击「近1年」后区间为 244 日', /（244 个交易日）/.test(status.textContent), status.textContent);
fire(bar, 'click', { target: btnAll });
check('点击「全部」恢复 886 日', /（886 个交易日）/.test(status.textContent), status.textContent);
check('点击后高亮转移到「全部」', btnAll.classList.contains('active') && !btn1Y.classList.contains('active'));

// 3) 平移
fire(bar, 'click', { target: buttons[0] }); // 近1月 -> [865, 885]
const before = status.textContent;
fire(bar, 'click', { target: buttons[4] }); // ‹ 左移
check('向左平移后区间起点前移', status.textContent !== before, before + '  =>  ' + status.textContent);
check('平移不改变窗口长度', /（21 个交易日）/.test(status.textContent), status.textContent);
fire(bar, 'click', { target: buttons[5] });
fire(bar, 'click', { target: buttons[5] });
check('向右平移到末端不越界', /至 2026-09-30（21 个交易日）/.test(status.textContent), status.textContent);

// 4) 框选缩放：全区间下从 x=700 拖到 x=800
fire(bar, 'click', { target: buttons[3] });
const sel = get('afgiSel');
fire(svg, 'mousedown', { clientX: 700, clientY: 100, preventDefault() {} });
fire(svg, 'mousemove', { clientX: 800, clientY: 100 });
check('拖拽中显示选区', sel.style.display === '' && parseFloat(sel.getAttribute('width')) > 0, 'width=' + sel.getAttribute('width'));
fireWin('mouseup', {});
check('框选后区间收窄', !/（886 个交易日）/.test(status.textContent), status.textContent);

// 5) 小幅拖拽（<12px）应被忽略
const keep = status.textContent;
fire(svg, 'mousedown', { clientX: 300, clientY: 100, preventDefault() {} });
fire(svg, 'mousemove', { clientX: 305, clientY: 100 });
fireWin('mouseup', {});
check('小于 12px 的拖拽不触发缩放', status.textContent === keep, status.textContent);

// 6) 双击复位
fire(svg, 'dblclick', {});
check('双击恢复全区间', /（886 个交易日）/.test(status.textContent), status.textContent);
check('双击后高亮同步到「全部」', btnAll.classList.contains('active'));

// 7) 悬停 tooltip
const tip = get('afgiTip');
fire(svg, 'mousemove', { clientX: 500, clientY: 120 });
check('悬停显示 tooltip', tip.style.display === 'block' && /AFGI/.test(tip.innerHTML), tip.innerHTML.replace(/<br\/>/g, ' | '));
fire(svg, 'mouseleave', {});
check('移出后隐藏 tooltip', tip.style.display === 'none');

// 8) window mouseup 只注册一次（重绘不应累积）
check('window mouseup 监听未累积', (win._handlers['mouseup'] || []).length === 1,
  'count=' + (win._handlers['mouseup'] || []).length);

console.log(failed ? `\n${failed} 项失败` : '\n全部通过');
process.exit(failed ? 1 : 0);
