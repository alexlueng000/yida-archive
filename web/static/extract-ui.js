// Date extraction runs on the server and keeps its status when the page reloads.
let extractTimer=null, extractLastStatus='';
const chinaToday=()=>{const p=Object.fromEntries(new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(x=>[x.type,x.value]));return p.year+'-'+p.month+'-'+p.day};
const extractInput=$('extractDate');
extractInput.max=chinaToday();
const yesterday=new Date(chinaToday()+'T00:00:00Z');yesterday.setUTCDate(yesterday.getUTCDate()-1);
extractInput.value=yesterday.toISOString().slice(0,10);
function showExtraction(job){
  const status=$('extractStatus'),running=job&&(job.status==='queued'||job.status==='running');
  $('extractStart').disabled=Boolean(running);$('extractDate').disabled=Boolean(running);
  if(!job){status.hidden=true;$('extractDetails').hidden=true;return}
  status.hidden=false;status.textContent=job.date+'：'+job.message;
  status.style.color=job.status==='failed'?'#9c3630':'';
  const lines=job.recent||[];$('extractDetails').hidden=!lines.length;$('extractLog').textContent=lines.join('\n');
  if((extractLastStatus==='running'||extractLastStatus==='queued')&&job.status==='succeeded'&&!$('home').hidden){
    api('/api/projects').then(r=>{projects=r.projects;renderProjects()}).catch(()=>{});
  }
  extractLastStatus=job.status;
  if(running){clearTimeout(extractTimer);extractTimer=setTimeout(refreshExtraction,3000)}
}
async function refreshExtraction(){
  try{const r=await api('/api/extraction');showExtraction(r.job)}
  catch(e){$('extractStatus').hidden=false;$('extractStatus').textContent='无法读取拉取状态：'+e.message;clearTimeout(extractTimer);extractTimer=setTimeout(refreshExtraction,10000)}
}
$('extractForm').onsubmit=async e=>{
  e.preventDefault();$('extractStart').disabled=true;
  try{const r=await api('/api/extraction',{date:extractInput.value});showExtraction(r.job);refreshExtraction()}
  catch(err){$('extractStart').disabled=false;$('extractStatus').hidden=false;$('extractStatus').textContent=err.message;$('extractStatus').style.color='#9c3630'}
};
refreshExtraction();

async function refreshSchedule(message=''){
  try{
    const r=await api('/api/schedule');
    $('scheduleTime').value=r.settings.time;
    let text=message||('当前设置：每天 '+r.settings.time+'（北京时间）。');
    text+=r.timer==='active'?' 定时服务正在运行。':r.timer==='inactive'?' 定时服务未运行，请检查服务器配置。':' 定时服务状态无法确认。';
    if(r.last_run){const label={running:'运行中',succeeded:'已完成',failed:'失败'}[r.last_run.status]||r.last_run.status;text+=' 最近一次：'+r.last_run.approval_date+'，'+label+'。'}
    $('scheduleStatus').textContent=text;
    $('scheduleStatus').style.color=r.timer==='inactive'?'#9c3630':'';
  }catch(e){$('scheduleStatus').textContent='无法读取定时设置：'+e.message;$('scheduleStatus').style.color='#9c3630'}
}
$('scheduleForm').onsubmit=async e=>{
  e.preventDefault();$('scheduleSave').disabled=true;
  try{await api('/api/schedule',{time:$('scheduleTime').value});await refreshSchedule('时间已保存。')}
  catch(e){$('scheduleStatus').textContent='保存失败：'+e.message;$('scheduleStatus').style.color='#9c3630'}
  finally{$('scheduleSave').disabled=false}
};
refreshSchedule();
