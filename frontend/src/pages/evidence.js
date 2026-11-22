import {heading, panel} from '../components/common/heading.js';
import {entries, records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

const REVIEW_COLUMNS = ['接收行', '到货批号', '分区流', '序号', '事件编号', '牌面编号', '动作', '内容', '状态', '依据'];

function reviewRows(rows) {
  return rows.map(row => REVIEW_COLUMNS.map(key => esc(row[key])));
}

function rowStateClass(row) {
  return {'已提交': 'row-applied', '重投忽略': 'row-duplicate', '矛盾隔离': 'row-conflict',
          '暂缓': 'row-pending', '历史未核实': 'row-history'}[row['状态']] || '';
}

export function evidencePage(state, identity) {
  const current = state.evidence.find(row=>row.id===identity) || state.evidence[0];
  if (!current) return heading(topic.modules[3]) + '<p class="empty">执行完成后，可查看、复核和导出证据。</p>';
  const {report} = current;
  const sectionMap = Object.fromEntries(report.sections.map(section=>[section.title, section.rows]));
  const journal = sectionMap['事件接收与处置'] || [];
  const progress = sectionMap['分批推进记录'] || [];
  const metrics = Object.entries(report.metrics).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  // 复核页三要素：首次收件行、跨分区/同流冲突、每批对账结果。
  const firstReceipts = journal.filter(row=>!String(row['依据']).includes('与接收行'));
  const conflicts = journal.filter(row=>row['状态']==='矛盾隔离');
  const dispositionClasses = journal.map(rowStateClass);
  const sections = report.sections
    .filter(section=>section.title !== '事件接收与处置')
    .map(x=>panel(x.title, entries(x.rows))).join('');
  const review = current.reviewer ? `<p data-testid="reviewed">${esc(current.reviewer)} · ${esc(date(current.reviewed_at))}</p><p>${esc(current.review_note)}</p>`
    : state.session.role==='reviewer'?'<form id="review-form"><label>复核意见<textarea name="note" maxlength="500" required></textarea></label><button>签署复核</button></form>'
    : '<p class="note">待复核员签署</p>';
  const history = state.evidence.map(row=>[`<a href="#evidence/${row.id}">执行 ${row.run_id}</a>`, esc(row.report.summary), esc(row.reviewer || '待复核')]);
  return `${heading(topic.modules[3], report.summary, `<button id="download" data-id="${current.id}">导出证据</button>`)}
    <div class="spread evidence-top"><dl class="metrics">${metrics}</dl><aside class="brief"><h2>复核签署</h2>${review}<details><summary>结果校验码</summary><code class="digest">${esc(current.digest)}</code></details></aside></div>
    ${panel('首次收件行（重投并入首次接收，不重复推进位点）', records(REVIEW_COLUMNS, reviewRows(firstReceipts), '无首次收件行。'))}
    ${panel('跨分区与同流冲突（各版本全部隔离，任一版均不推进位点）', records(REVIEW_COLUMNS, reviewRows(conflicts), '无冲突信封。'))}
    ${panel('每批对账结果（按分区流记录原位点、提交数与首个阻塞）', entries(progress))}
    ${panel('牌面发布单与消费位点', sections)}
    ${panel('事件接收与处置（全量逐条）', records(REVIEW_COLUMNS, reviewRows(journal), '无到货事件。', dispositionClasses))}
    <details><summary>历史证据</summary>${records(['执行','结论','复核'], history)}</details>`;
}

export function bindEvidence(api, update, fail) {
  const button = document.getElementById('download');
  if (!button) return;
  const identity = button.dataset.id;
  button.addEventListener('click', async()=>{
    try {
      const response = await api(`/evidence/${identity}/download`, {response:true});
      const url = URL.createObjectURL(await response.blob());
      const link = el('a');
      link.href = url;
      link.download = `evidence-${identity}.json`;
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
  });
  submit('review-form', async data=>{
    await api(`/evidence/${identity}/review`, {method:'POST', data:{note:data.get('note')}});
    await update();
  }, fail);
}
