let sourceId='',sourcePath='',sourceEntries=[],sourceList=[],sourceRequest=0;
let classifyToken=0,classifyTimer=null,currentHistory=null;
function historyLabel(h){if(!h||!h.completed_at)return '';return '已归类 · '+date(h.completed_at)+(h.rejected?' · '+h.rejected+' 份未归档':'')+'（目录后续可能有变动）'}
function sourceLink(path,view=false){return '/api/source/file?id='+encodeURIComponent(sourceId)+'&path='+encodeURIComponent(path)+(view?'&view=1':'')}
async function openSources(id='',path=''){
  if(busy)return;
  const request=++sourceRequest;
  ++classifyToken;clearTimeout(classifyTimer);$('sourceClassifyStatus').hidden=true;
  $('sourceClassify').hidden=!id||!path;$('sourceClassify').disabled=true;
  $('sourceSearch').value='';$('sourceList').innerHTML='<div class="empty">正在读取原目录…</div>';
  $('sourceView').hidden=false;$('home').hidden=true;$('detail').hidden=true;$('notice').hidden=true;
  current=null;sourceId=id;sourcePath=path;
  location.hash=id?'source='+encodeURIComponent(id)+'&dir='+encodeURIComponent(path):'browse=1';
  try{
    const result=await api(id?'/api/source/tree?id='+encodeURIComponent(id)+'&path='+encodeURIComponent(path):'/api/sources');
    if(request!==sourceRequest||$('sourceView').hidden)return;
    sourceEntries=result.entries||[];sourceList=result.sources||[];currentHistory=result.classification||null;
    if(id){
      sourcePath=result.path;
      const parts=sourcePath?sourcePath.split('/'):[];
      $('sourceCrumbs').innerHTML='<button data-source-home>全部原目录</button><span>/</span><button data-source-path="">'+esc(result.source.name)+'</button>'+parts.map((name,index)=>'<span>/</span><button data-source-path="'+esc(parts.slice(0,index+1).join('/'))+'">'+esc(name)+'</button>').join('');
      $('sourcePath').textContent='原始位置：'+result.source.path+(sourcePath?' / '+sourcePath:'');
      $('sourceSummary').textContent=sourceEntries.filter(e=>e.kind==='directory').length+' 个文件夹 · '+sourceEntries.filter(e=>e.kind==='file').length+' 个文件'+(historyLabel(currentHistory)?' · '+historyLabel(currentHistory):'');
    }else{
      $('sourceCrumbs').textContent='全部原目录';$('sourcePath').textContent='按原始上传目录查看资料，保留阶段名称和原文件名。';
      $('sourceSummary').textContent=sourceList.length+' 个项目原目录';
    }
    renderSources();
    if(id){$('sourceClassify').disabled=false;restoreClassification()}
  }catch(e){if(request===sourceRequest){$('sourceList').innerHTML='<div class="empty">无法读取目录，请刷新重试。</div>';notice(e.message,true)}}
}
function showClassification(job){
  const active=job.status==='queued'||job.status==='running',failed=job.status==='failed';
  $('sourceClassify').disabled=active;
  const panel=$('sourceClassifyStatus');panel.hidden=false;panel.className='notice'+(failed?' error':'');
  if(job.status==='succeeded'){
    const result=job.result,s=result.summary;
    panel.innerHTML='<strong>归类完成</strong>：读取 '+s.total+' 个附件，同内容合并 '+s.duplicates+' 份。项目当前保存 '+result.files.length+' 份文件，其中 '+result.pending+' 份未归类。'+(s.rejected.length?'\n未归档：'+s.rejected.map(f=>esc(f.name+'（'+f.error+'）')).join('；'):'')+'\n保存位置：'+esc(result.path)+'\n<a href="#project='+encodeURIComponent(result.project.id)+'">查看归档结果 →</a>';
  }else{
    panel.textContent=(job.project?'项目 '+job.project.project_no+'：':'')+job.message;
    if(active){const progress=document.createElement('progress');progress.max=100;progress.value=job.progress;progress.style.cssText='display:block;width:100%;max-width:400px;margin-top:10px';progress.setAttribute('aria-label','归类进度');panel.append(progress)}
  }
  return active;
}
async function pollClassification(job,token){
  if(token!==classifyToken||$('sourceView').hidden)return;
  if(!showClassification(job))return;
  classifyTimer=setTimeout(async()=>{try{const r=await api('/api/source/classify-status?job='+encodeURIComponent(job.id));if(token===classifyToken)pollClassification(r.job,token)}catch(e){if(token===classifyToken){$('sourceClassify').disabled=false;$('sourceClassifyStatus').textContent='进度获取失败，请点击「刷新目录」重新查看。'}}},1000);
}
async function restoreClassification(){
  const token=classifyToken,id=sourceId,path=sourcePath;
  try{const r=await api('/api/source/classify-status?id='+encodeURIComponent(id)+'&path='+encodeURIComponent(path));if(token===classifyToken&&r.job)pollClassification(r.job,token)}catch(e){/* Directory browsing remains available if the task history is unavailable. */}
}
async function classifySource(){
  if(!sourceId||!sourcePath||$('sourceClassify').disabled)return;
  const token=++classifyToken;clearTimeout(classifyTimer);$('sourceClassify').disabled=true;
  $('sourceClassifyStatus').hidden=false;$('sourceClassifyStatus').className='notice';$('sourceClassifyStatus').textContent='正在启动当前目录及子目录的归类…';
  try{const r=await api('/api/source/classify',{id:sourceId,path:sourcePath});if(token===classifyToken)pollClassification(r.job,token)}catch(e){if(token===classifyToken){$('sourceClassify').disabled=false;$('sourceClassifyStatus').className='notice error';$('sourceClassifyStatus').textContent=e.message}}
}
function renderSources(){
  const q=$('sourceSearch').value.trim().toLowerCase();
  if(!sourceId){
    const rows=sourceList.filter(s=>s.name.toLowerCase().includes(q));
    $('sourceList').innerHTML=rows.length?'<table><thead><tr><th>项目原目录</th><th>目录时间</th><th>操作</th></tr></thead><tbody>'+rows.map(s=>'<tr><td class="file-name"><button class="link-button" data-source-id="'+esc(s.id)+'">▣ '+esc(s.name)+'</button><div class="sub">每次上传</div></td><td>'+date(s.modified)+'</td><td><button class="link-button" data-source-id="'+esc(s.id)+'">浏览目录 →</button></td></tr>').join('')+'</tbody></table>':'<div class="empty"><strong>'+(q?'未找到匹配的项目':'还没有可浏览的原目录')+'</strong>已下载到本机的各项目「每次上传」目录会显示在这里。</div>';
    return;
  }
  const rows=sourceEntries.filter(e=>e.name.toLowerCase().includes(q));
  $('sourceList').innerHTML=rows.length?'<table><thead><tr><th>名称</th><th>类型 / 数量</th><th>大小</th><th>修改时间</th><th>操作</th></tr></thead><tbody>'+rows.map(e=>'<tr><td class="file-name">'+(e.kind==='directory'?'<button class="link-button source-folder" data-source-path="'+esc(e.path)+'">▣ '+esc(e.name)+'</button>'+(historyLabel(e.classification)?'<div><span class="classification-history">✓ '+esc(historyLabel(e.classification))+'</span></div>':''):esc(e.name))+'</td><td>'+(e.kind==='directory'?e.folders+' 个文件夹 · '+e.files+' 个文件':esc(e.name.split('.').length>1?e.name.split('.').pop().toUpperCase()+' 文件':'文件'))+'</td><td>'+(e.kind==='directory'?'—':size(e.size))+'</td><td>'+date(e.modified)+'</td><td>'+(e.kind==='directory'?'<button class="link-button" data-source-path="'+esc(e.path)+'">打开目录 →</button>':(e.preview?'<a target="_blank" rel="noopener" href="'+sourceLink(e.path,true)+'">查看</a> &nbsp; ':'')+'<a href="'+sourceLink(e.path)+'">下载</a>')+'</td></tr>').join('')+'</tbody></table>':'<div class="empty"><strong>'+(q?'没有匹配的文件或目录':'此目录为空')+'</strong>'+(q?'试试其他文件名。':'可以通过上方路径返回上一级。')+'</div>';
}
async function browseProjectSource(){
  if(busy||!current)return;
  const project=current.project_no;
  try{const r=await api('/api/sources');const source=r.sources.find(s=>s.name===project+'-全流程')||r.sources.find(s=>s.name===project);await openSources(source?source.id:'')}
  catch(e){notice(e.message,true)}
}
$('sourcesNav').onclick=()=>openSources();$('sourceHomeBtn').onclick=home;$('sourceRefresh').onclick=()=>openSources(sourceId,sourcePath);$('sourceSearch').oninput=renderSources;$('browseSourceBtn').onclick=browseProjectSource;
$('sourceClassify').onclick=classifySource;
for(const id of ['sourceList','sourceCrumbs'])$(id).onclick=e=>{const node=e.target.closest('[data-source-id],[data-source-path],[data-source-home]');if(!node)return;if(node.hasAttribute('data-source-home'))openSources();else if(node.hasAttribute('data-source-id'))openSources(node.dataset.sourceId);else openSources(sourceId,node.dataset.sourcePath)};
window.addEventListener('hashchange',()=>{if(busy)return;const params=new URLSearchParams(location.hash.slice(1));if(params.has('source')){const id=params.get('source'),path=params.get('dir')||'';if($('sourceView').hidden||id!==sourceId||path!==sourcePath)openSources(id,path)}else if(params.has('browse')&&($('sourceView').hidden||sourceId))openSources()});
const sourceInitial=new URLSearchParams(location.hash.slice(1));if(sourceInitial.has('source'))openSources(sourceInitial.get('source'),sourceInitial.get('dir')||'');else if(sourceInitial.has('browse'))openSources();
