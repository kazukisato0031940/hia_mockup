/* ========================================
   HIA 疾患予測・受診勧奨 機能スクリプト
   ======================================== */

var PERSONS=[
  {id:'#00124',name:'田中 次郎',age:52,sex:'男',risk:'ckd',riskLabel:'腎疾患(CKD)',  basis:'eGFR 42.1 / 蛋白尿(+)',        score:92,prob:'48%',prio:'E',score24:84,prio24:'E', status:'unsent',notifyCount:3},
  {id:'#00056',name:'鈴木 一郎',age:61,sex:'男',risk:'dm', riskLabel:'糖尿病重症化', basis:'HbA1c 8.2 / 未受診',           score:88,prob:'41%',prio:'E',score24:80,prio24:'E', status:'unsent',notifyCount:2},
  {id:'#00089',name:'健保 太郎',age:55,sex:'男',risk:'dm', riskLabel:'糖尿病重症化', basis:'HbA1c 7.5 / BMI 29.0',         score:84,prob:'36%',prio:'E',score24:75,prio24:'E', status:'unsent',notifyCount:1},
  {id:'#00312',name:'伊藤 誠',  age:58,sex:'男',risk:'ckd',riskLabel:'腎疾患(CKD)',  basis:'eGFR 48.0 / 血圧 145mmHg',     score:79,prob:'30%',prio:'E',score24:61,prio24:'D', status:'unsent',notifyCount:0},
  {id:'#00201',name:'山田 花子',age:48,sex:'女',risk:'cv', riskLabel:'循環器リスク', basis:'収縮期血圧 158 / LDL 178',      score:76,prob:'29%',prio:'D',score24:68,prio24:'D', status:'unsent',notifyCount:1},
  {id:'#00445',name:'小林 陽子',age:44,sex:'女',risk:'cv', riskLabel:'循環器リスク', basis:'LDL 192 / 収縮期 150mmHg',      score:71,prob:'24%',prio:'D',score24:62,prio24:'D', status:'unsent',notifyCount:0},
  {id:'#00078',name:'渡辺 健',  age:50,sex:'男',risk:'dm', riskLabel:'糖尿病重症化', basis:'HbA1c 7.1 / アドヒアランス低', score:64,prob:'18%',prio:'D',score24:55,prio24:'C', status:'unsent',notifyCount:2},
  {id:'#00509',name:'中村 美咲',age:39,sex:'女',risk:'dm', riskLabel:'糖尿病重症化', basis:'HbA1c 7.0 / 空腹時血糖 118',   score:58,prob:'13%',prio:'C',score24:null,prio24:null,status:'unsent',notifyCount:0}
];
var selIds=new Set();

function showPage(id,el){
  ['dashboard','list','mail'].forEach(function(p){
    var pg=document.getElementById('page-hia-'+p);
    if(pg) pg.classList.toggle('active', id===p);
  });
  var pf=document.getElementById('pagerFloat');
  if(pf){
    if(id!=='list'){
      pf.classList.add('hidden');
    } else {
      renderPage();
    }
  }
}

function goToList(risk){
  showPage('list', null);
  document.getElementById('fRisk').value = risk==='all' ? 'all' : risk;
  document.getElementById('fPrio').value = 'all';
  document.getElementById('fStat').value = 'all';
  document.getElementById('fSearch').value = '';
  applyFilter();
}

function rBadge(r){var m={dm:['b-red','糖尿病重症化'],ckd:['b-blue','腎疾患(CKD)'],cv:['b-amber','循環器']};return '<span class="badge '+m[r][0]+'">'+m[r][1]+'</span>';}
function pBadge(p){
  if(p==='A') return '<span class="prio-a">A（良好）</span>';
  if(p==='B') return '<span class="prio-b">B（経過観察）</span>';
  if(p==='C') return '<span class="prio-c">C（要生活改善）</span>';
  if(p==='D') return '<span class="prio-d">D（要治療）</span>';
  return '<span class="prio-e">E（要受診勧奨）</span>';
}
function sBadge(s){return s==='sent'?'<span class="st-sent"><span class="sdot"></span>勧奨済</span>':'<span class="st-unsent"><span class="sdot"></span>未勧奨</span>';}
function sBar(sc){var c=sc>=80?'#e53e3e':'#d97706';return '<div style="display:flex;align-items:center;gap:5px;"><div class="sbar-wrap"><div class="sbar" style="width:'+sc+'%;background:'+c+'"></div></div><span style="font-size:12px;font-weight:bold;color:'+c+'">'+sc+'</span></div>';}
function scoreDiff(cur, prev){
  if(prev===null) return '<span style="font-size:11px;color:#a0aec0;">前年データなし</span>';
  var diff=cur-prev;
  var c=diff>0?'#e53e3e':diff<0?'#38a169':'#a0aec0';
  var sign=diff>0?'▲ +':'▼ ';
  return '<div style="font-size:11px;margin-top:3px;color:'+c+';">前年: '+prev+'　'+(diff===0?'±0':sign+Math.abs(diff))+'</div>';
}
function prioChange(cur, prev){
  if(prev===null) return '<span style="font-size:11px;color:#a0aec0;">—</span>';
  var order={A:0,B:1,C:2,D:3,E:4};
  var diff=order[cur]-order[prev];
  if(diff===0) return '<span style="font-size:12px;color:#a0aec0;">→ 変化なし</span>';
  if(diff<0){
    return '<span style="font-size:12px;font-weight:bold;color:#e53e3e;">'+prev+' → '+cur+' ▲悪化</span>';
  }
  return '<span style="font-size:12px;font-weight:bold;color:#38a169;">'+prev+' → '+cur+' ▼改善</span>';
}
var PAGE_SIZE = 10;
var hiaCurrentPage = 1;
var filteredData = [];

function renderTable(data){
  filteredData = data;
  hiaCurrentPage = 1;
  renderPage();
}

function renderPage(){
  var tb = document.getElementById('rtbody');
  var total = filteredData.length;
  var totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if(hiaCurrentPage > totalPages) hiaCurrentPage = totalPages;

  var start = (hiaCurrentPage - 1) * PAGE_SIZE;
  var pageData = filteredData.slice(start, start + PAGE_SIZE);

  if(!total){
    tb.innerHTML='<tr><td colspan="11" style="text-align:center;color:#a0aec0;padding:20px;">該当者なし</td></tr>';
    document.getElementById('pagerFloat').classList.add('hidden');
    return;
  }

  tb.innerHTML=pageData.map(function(d){
    var ck=selIds.has(d.id)?'checked':'';
    var nc=d.notifyCount||0;
    var ncColor=nc===0?'#a0aec0':nc>=3?'#e53e3e':'#d97706';
    return '<tr class="'+(selIds.has(d.id)?'selected':'')+'">'
      +'<td style="text-align:center;"><input type="checkbox" class="chk" '+ck+' onchange="toggle(\''+d.id+'\',this)"></td>'
      +'<td><div class="fw">'+d.name+'</div><div style="font-size:11px;color:#a0aec0;">'+d.id+'</div></td>'
      +'<td style="text-align:center;">'+d.age+'歳</td>'
      +'<td>'+rBadge(d.risk)+'</td>'
      +'<td style="font-size:11px;color:var(--txt2);line-height:1.6;white-space:normal;">'+d.basis.replace(' / ','<br>')+'</td>'
      +'<td>'+sBar(d.score)+scoreDiff(d.score,d.score24)+'</td>'
      +'<td style="text-align:center;font-size:13px;font-weight:bold;color:'+(d.score>=80?'#e53e3e':'#d97706')+';">'+d.prob+'</td>'
      +'<td style="text-align:center;">'+pBadge(d.prio)+'</td>'
      +'<td style="text-align:center;">'+prioChange(d.prio,d.prio24)+'</td>'
      +'<td style="text-align:center;font-size:13px;font-weight:bold;color:'+ncColor+';">'+nc+'回</td>'
      +'<td style="text-align:center;">'+sBadge(d.status)+'</td>'
      +'</tr>';
  }).join('');

  /* ページャー更新 */
  var pf = document.getElementById('pagerFloat');
  pf.classList.toggle('hidden', totalPages <= 1);

  document.getElementById('pagerPrev').disabled = hiaCurrentPage <= 1;
  document.getElementById('pagerNext').disabled = hiaCurrentPage >= totalPages;
  document.getElementById('pagerInfo').textContent =
    (start+1)+'–'+Math.min(start+PAGE_SIZE, total)+' / '+total+'件';

  /* ページ番号ボタン */
  var nums = document.getElementById('pagerNums');
  var pages = buildPageNums(hiaCurrentPage, totalPages);
  nums.innerHTML = pages.map(function(p){
    if(p === '…') return '<span class="pager-info">…</span>';
    return '<button class="'+(p===hiaCurrentPage?'active':'')+'" onclick="jumpPage('+p+')">'+ p +'</button>';
  }).join('');

  updSel();
}

function buildPageNums(cur, total){
  if(total <= 7) {
    var arr=[]; for(var i=1;i<=total;i++) arr.push(i); return arr;
  }
  var pages = [1];
  if(hiaCurrentPage > 3) pages.push('…');
  for(var p=Math.max(2,cur-1); p<=Math.min(total-1,cur+1); p++) pages.push(p);
  if(cur < total-2) pages.push('…');
  pages.push(total);
  return pages;
}

function goPage(dir){
  var total = Math.ceil(filteredData.length / PAGE_SIZE);
  hiaCurrentPage = Math.max(1, Math.min(total, hiaCurrentPage + dir));
  renderPage();
}

function jumpPage(p){
  hiaCurrentPage = p;
  renderPage();
}

function applyFilter(){
  var r=document.getElementById('fRisk').value,p=document.getElementById('fPrio').value,s=document.getElementById('fStat').value,q=document.getElementById('fSearch').value.toLowerCase();
  renderTable(PERSONS.filter(function(d){return (r==='all'||d.risk===r)&&(p==='all'||d.prio===p)&&(s==='all'||d.status===s)&&(!q||d.name.toLowerCase().indexOf(q)>=0||d.id.indexOf(q)>=0);}));
}
function hiaToggleAll(cb){
  if(cb.checked){ PERSONS.forEach(function(d){selIds.add(d.id);}); }
  else { selIds.clear(); }
  applyFilter();
}
function toggle(id,cb){
  if(cb.checked) selIds.add(id); else selIds.delete(id);
  // ヘッダーcheckbox の状態を同期
  var all=document.getElementById('chkAll');
  if(all) all.checked = selIds.size===PERSONS.length;
  applyFilter();
}
function updSel(){
  var n=selIds.size;
  var sc=document.getElementById('selCount');
  if(sc) sc.textContent=n+'名 選択中';
}


function switchTab(id,el){document.querySelectorAll('.rp-tab').forEach(function(t){t.classList.remove('active');});document.querySelectorAll('.rp-pane').forEach(function(p){p.classList.remove('active');});el.classList.add('active');document.getElementById('pane-'+id).classList.add('active');}
function showWarn(title, msg){
  document.getElementById('warnTitle').textContent = title;
  document.getElementById('warnMsg').textContent = msg;
  document.getElementById('warnModal').classList.remove('hidden');
}
function openMailModal(){
  if(!selIds.size){
    showWarn('対象者が選択されていません', '送信するには、一覧からチェックボックスで対象者を選択してください。');
    return;
  }
  document.getElementById('mSel').textContent=selIds.size+'名';
  document.getElementById('mSubj').textContent='—';
  document.getElementById('mSched2').textContent='即時送信';
  document.getElementById('sendModal').classList.remove('hidden');
}
function confirmSend(){
  closeModal();
  var n=selIds.size;
  selIds.forEach(function(id){var p=PERSONS.find(function(d){return d.id===id;});if(p)p.status='sent';});
  selIds.clear();applyFilter();
  var sent=PERSONS.filter(function(d){return d.status==='sent';}).length;
  document.getElementById('kpiStatus').textContent='未勧奨: '+(PERSONS.length-sent)+'名 ／ 勧奨済: '+sent+'名';
  var tb=document.getElementById('sendHist');if(tb.querySelector('td[colspan]'))tb.innerHTML='';
  var tr=document.createElement('tr');
  tr.innerHTML='<td>'+new Date().toLocaleString('ja-JP')+'</td><td>受診勧奨メール</td><td>'+n+'名</td><td>メール</td><td><span class="status-badge status-success">送信完了</span></td>';
  tb.insertBefore(tr,tb.firstChild);
}
function closeModal(){document.getElementById('sendModal').classList.add('hidden');}

/* ── テンプレートモーダル ── */
var editingTplIdx = null;
function openTplModal(idx){
  editingTplIdx = idx;
  if(idx !== null){
    var t=savedTpls[idx];
    document.getElementById('tplModalTitle').textContent='テンプレート編集';
    document.getElementById('tplName').value=t.name;
    document.getElementById('tplCategory').value=t.category||'';
    document.getElementById('tplSubject').value=t.subject;
    document.getElementById('tplBody').value=t.body;
  } else {
    document.getElementById('tplModalTitle').textContent='テンプレート作成';
    clearTplForm();
  }
  document.getElementById('tplSaveMeta').textContent='';
  document.getElementById('tplModal').classList.remove('hidden');
}
function closeTplModal(){document.getElementById('tplModal').classList.add('hidden');}
function closePreviewModal(){document.getElementById('previewModal').classList.add('hidden');}

var PRESETS={
  dm:{name:'糖尿病重症化リスク 受診勧奨',category:'dm',subject:'【重要】健診結果に基づく受診のご案内（糖尿病）',body:'{{氏名}} 様\n\nHbA1c値が基準値を超えており、糖尿病の重症化リスクが検出されました（リスクスコア: {{スコア}}）。\n放置すると合併症（網膜症・腎症・神経障害）のリスクがあります。\n\n内科・糖尿病専門外来への早めの受診をお勧めします。\n\n○○健康保険組合'},
  ckd:{name:'腎疾患(CKD) 受診勧奨',category:'ckd',subject:'【重要】健診結果に基づく受診のご案内（腎疾患）',body:'{{氏名}} 様\n\neGFR値の低下が確認され、腎機能低下（慢性腎臓病）のリスクが検出されました（リスクスコア: {{スコア}}）。\n早期の治療が透析移行を防ぐために有効です。\n\n腎臓内科または内科への受診をお勧めします。\n\n○○健康保険組合'},
  cv:{name:'循環器リスク 受診勧奨',category:'cv',subject:'【重要】健診結果に基づく受診のご案内（循環器）',body:'{{氏名}} 様\n\n血圧・コレステロール値が高く、循環器系疾患のリスクが検出されました（リスクスコア: {{スコア}}）。\n脳卒中・心筋梗塞のリスクが高まっています。\n\n循環器内科または内科への受診をお勧めします。\n\n○○健康保険組合'}
};
var savedTpls=[
  {name:'糖尿病重症化リスク 受診勧奨',category:'dm',subject:'【重要】健診結果に基づく受診のご案内（糖尿病）',body:'{{氏名}} 様\n\nHbA1c値が基準値を超えており、糖尿病の重症化リスクが検出されました（リスクスコア: {{スコア}}）。\n放置すると合併症（網膜症・腎症・神経障害）のリスクがあります。\n\n内科・糖尿病専門外来への早めの受診をお勧めします。\n\n○○健康保険組合',updatedAt:'2026/03/10 09:15'},
  {name:'腎疾患(CKD) 受診勧奨',category:'ckd',subject:'【重要】健診結果に基づく受診のご案内（腎疾患）',body:'{{氏名}} 様\n\neGFR値の低下が確認され、腎機能低下（慢性腎臓病）のリスクが検出されました（リスクスコア: {{スコア}}）。\n早期の治療が透析移行を防ぐために有効です。\n\n腎臓内科または内科への受診をお勧めします。\n\n○○健康保険組合',updatedAt:'2026/03/10 09:20'},
  {name:'循環器リスク 受診勧奨',category:'cv',subject:'【重要】健診結果に基づく受診のご案内（循環器）',body:'{{氏名}} 様\n\n血圧・コレステロール値が高く、循環器系疾患のリスクが検出されました（リスクスコア: {{スコア}}）。\n脳卒中・心筋梗塞のリスクが高まっています。\n\n循環器内科または内科への受診をお勧めします。\n\n○○健康保険組合',updatedAt:'2026/03/10 09:25'},
  {name:'全疾患共通 受診勧奨',category:'all',subject:'【重要】健診結果に基づく受診のご案内',body:'{{氏名}} 様\n\n健診結果および疾患予測AIによる分析の結果、{{予測疾患}} のリスクが確認されました（リスクスコア: {{スコア}}）。\n\n早めの受診・検査をお勧めいたします。\nかかりつけ医、または最寄りの医療機関にご相談ください。\n\n○○健康保険組合',updatedAt:'2026/03/10 09:30'},
];
var tplHists={
  '糖尿病重症化リスク 受診勧奨':[
    {dt:'2026/03/18 10:45',target:'12名',method:'メール'},
    {dt:'2025/10/02 14:20',target:'9名', method:'メール'},
  ],
  '腎疾患(CKD) 受診勧奨':[
    {dt:'2026/03/18 10:50',target:'5名',method:'メール'},
  ],
};

function renderTplList(){
  var tbody=document.getElementById('tplListArea');
  if(!savedTpls.length){
    tbody.innerHTML='<tr><td colspan="7" style="color:#a0aec0;font-size:12px;padding:24px;">テンプレートがまだ保存されていません。「テンプレート作成」から追加してください。</td></tr>';
    return;
  }
  var catLabel={dm:'糖尿病',ckd:'腎疾患',cv:'循環器',all:'全疾患共通','':'—'};
  tbody.innerHTML=savedTpls.map(function(t,i){
    var histCount=(tplHists[t.name]||[]).length;
    return '<tr>'
      +'<td style="text-align:center;font-size:11px;color:#a0aec0;white-space:nowrap;">'+t.updatedAt+'</td>'
      +'<td style="font-weight:bold;">'+t.name+'</td>'
      +'<td style="text-align:center;">'+(t.category?'<span style="font-size:11px;background:#bee3f8;color:#2c5282;padding:2px 8px;border-radius:12px;">'+catLabel[t.category]+'</span>':'—')+'</td>'
      +'<td style="font-size:11px;color:var(--txt2);">'+t.subject+'</td>'
      +'<td style="text-align:center;white-space:nowrap;"><button class="btn-sm" onclick="openTplHist('+i+')">'+(histCount?histCount+'件':'—')+'</button></td>'
      +'<td style="text-align:center;white-space:nowrap;"><button class="btn-sm" onclick="previewTpl('+i+')">プレビュー</button></td>'
      +'<td style="text-align:center;white-space:nowrap;">'
      +'<button class="btn-edit" onclick="openTplModal('+i+')" style="margin-right:4px;">編集</button>'
      +'<button class="btn-sm" onclick="deleteTpl('+i+')">削除</button>'
      +'</td>'
      +'</tr>';
  }).join('');
}

function openTplHist(i){
  var t=savedTpls[i];
  var hists=tplHists[t.name]||[];
  document.getElementById('tplHistTitle').textContent='送信履歴：'+t.name;
  var tb=document.getElementById('tplHistBody');
  if(!hists.length){
    tb.innerHTML='<tr><td colspan="4" style="color:#a0aec0;font-size:12px;">送信履歴はありません</td></tr>';
  } else {
    tb.innerHTML=hists.map(function(h){
      return '<tr><td style="white-space:nowrap;">'+h.dt+'</td><td>'+h.target+'</td><td>'+h.method+'</td><td><span class="status-badge status-success">送信完了</span></td></tr>';
    }).join('');
  }
  document.getElementById('tplHistModal').classList.remove('hidden');
}
function loadPreset(key){
  var p=PRESETS[key];
  document.getElementById('tplName').value=p.name;
  document.getElementById('tplCategory').value=p.category;
  document.getElementById('tplSubject').value=p.subject;
  document.getElementById('tplBody').value=p.body;
}
function clearTplForm(){
  document.getElementById('tplName').value='';
  document.getElementById('tplCategory').value='';
  document.getElementById('tplSubject').value='';
  document.getElementById('tplBody').value='';
  document.getElementById('tplSaveMeta').textContent='';
}
function saveTpl(){
  var name=document.getElementById('tplName').value.trim();
  var subj=document.getElementById('tplSubject').value.trim();
  var body=document.getElementById('tplBody').value.trim();
  var cat=document.getElementById('tplCategory').value;
  if(!name||!body){alert('テンプレート名と本文は必須です。');return;}
  var tpl={name:name,category:cat,subject:subj,body:body,updatedAt:new Date().toLocaleString('ja-JP')};
  if(editingTplIdx!==null){savedTpls[editingTplIdx]=tpl;}
  else{
    var existing=savedTpls.findIndex(function(t){return t.name===name;});
    if(existing>=0){savedTpls[existing]=tpl;}else{savedTpls.push(tpl);}
  }
  document.getElementById('tplSaveMeta').textContent='✓ 保存しました（'+tpl.updatedAt+'）';
  renderTplList();
  setTimeout(closeTplModal, 800);
}
function previewTpl(i){
  var t=savedTpls[i];
  document.getElementById('previewSubjectOut').textContent=t.subject||'—';
  document.getElementById('previewBodyOut').textContent=t.body||'—';
  document.getElementById('previewModal').classList.remove('hidden');
}
function deleteTpl(i){
  var name = savedTpls[i] && savedTpls[i].name;
  if(!name) return;
  if(!confirm('「'+name+'」を削除しますか？')) return;
  delete tplHists[name];
  savedTpls.splice(i, 1);
  editingTplIdx = null;
  renderTplList();
}
function renderPreview(){
  var subj=document.getElementById('tplSubject').value;
  var body=document.getElementById('tplBody').value;
  var name=document.getElementById('previewName').value||'（氏名）';
  var dis=document.getElementById('previewDisease').value||'（予測疾患）';
  var sc=document.getElementById('previewScore').value||'（スコア）';
  var replaced=body.replace(/\{\{氏名\}\}/g,name).replace(/\{\{予測疾患\}\}/g,dis).replace(/\{\{スコア\}\}/g,sc);
  var subjReplaced=subj.replace(/\{\{氏名\}\}/g,name).replace(/\{\{予測疾患\}\}/g,dis).replace(/\{\{スコア\}\}/g,sc);
  document.getElementById('previewSubjectOut').textContent=subjReplaced||'—';
  document.getElementById('previewBodyOut').textContent=replaced||'本文が入力されていません。';
}

function exportCSV(){
  var targets = PERSONS.filter(function(d){ return selIds.has(d.id); });
  if(!targets.length){
    showWarn('対象者が選択されていません', 'CSVを出力するには、一覧からチェックボックスで対象者を選択してください。');
    return;
  }
  var rows=[['氏名','被保険者番号','年齢','性別','予測疾患','リスクスコア','判定','5年内発症確率','主要健診値','勧奨回数','勧奨状況']];
  targets.forEach(function(d){
    rows.push([d.name,d.id,d.age,d.sex,d.riskLabel,d.score,d.prio,d.prob,d.basis,d.notifyCount||0,d.status==='sent'?'勧奨済':'未勧奨']);
  });
  var csv=rows.map(function(r){
    return r.map(function(c){return '"'+String(c).replace(/"/g,'""')+'"';}).join(',');
  }).join('\n');
  var blob=new Blob(['\uFEFF'+csv],{type:'text/csv;charset=utf-8;'});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');
  a.href=url;
  a.download='受診勧奨対象者リスト.csv';
  a.style.display='none';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

var R25={all:[76,68,72,85,58],dm:[85,70,65,60,50],cv:[60,78,80,55,74],ckd:[65,72,55,90,52]};
var R24={all:[68,63,65,78,52],dm:[76,65,60,54,46],cv:[54,72,74,50,68],ckd:[60,66,50,83,48]};
var radarInst=null;

function initCharts(){
  radarInst=new Chart(document.getElementById('radarChart').getContext('2d'),{
    type:'radar',
    data:{labels:['糖尿病','高血圧','脂質異常','腎疾患','心血管'],datasets:[
      {label:'2025年度',data:R25.all,borderColor:'#3182ce',backgroundColor:'rgba(49,130,206,0.15)',pointBackgroundColor:'#3182ce',pointRadius:4,borderWidth:2},
      {label:'2024年度',data:R24.all,borderColor:'#f6ad55',borderDash:[5,4],backgroundColor:'rgba(246,173,85,0.08)',pointBackgroundColor:'#f6ad55',pointRadius:3,borderWidth:1.5},
      {label:'全国平均',data:[60,60,60,60,60],borderColor:'#b0b8c1',borderDash:[3,3],backgroundColor:'rgba(176,184,193,0.06)',pointBackgroundColor:'#b0b8c1',pointRadius:2,borderWidth:1}
    ]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{r:{min:45,max:90,ticks:{stepSize:5,font:{size:10},color:'#a0aec0'},pointLabels:{font:{size:12},color:'#626262'},grid:{color:'rgba(0,0,0,0.07)'},angleLines:{color:'rgba(0,0,0,0.07)'}}}}
  });
  var barOpts={responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{grid:{display:false},ticks:{font:{size:10},color:'#626262'}},y:{beginAtZero:true,ticks:{stepSize:2,font:{size:10},color:'#a0aec0'},grid:{color:'rgba(0,0,0,0.06)'},title:{display:true,text:'件数 / 1,000人年',font:{size:9},color:'#a0aec0'}}}};

  new Chart(document.getElementById('barChartNone').getContext('2d'),{
    type:'bar',
    data:{labels:['心疾患イベント','脳血管イベント','人工透析導入'],datasets:[
      {label:'2024年度 対策なし',data:[18,14,7],backgroundColor:'#a0aec0',borderRadius:3},
      {label:'2025年度 対策なし',data:[15,12,5],backgroundColor:'#718096',borderRadius:3}
    ]},
    options:barOpts
  });

  new Chart(document.getElementById('barChartInter').getContext('2d'),{
    type:'bar',
    data:{labels:['心疾患イベント','脳血管イベント','人工透析導入'],datasets:[
      {label:'2024年度 早期介入',data:[13,11,5],backgroundColor:'#68d391',borderRadius:3},
      {label:'2025年度 早期介入',data:[11,9,4], backgroundColor:'#276749',borderRadius:3}
    ]},
    options:barOpts
  });
}

function updateRadar(){
  if(!radarInst)return;
  var k=document.getElementById('radarFilter').value;
  radarInst.data.datasets[0].data=R25[k];
  radarInst.data.datasets[1].data=R24[k];
  radarInst.update();
}