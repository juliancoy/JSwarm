const section = document.createElement('section');
section.id = 'deployment-progress';
section.setAttribute('aria-labelledby', 'deployment-heading');
section.innerHTML = `<h2 id="deployment-heading">Deployment progress and queue</h2>
<p id="deployment-status" role="status">Checking deployments…</p><div id="deployment-attempts"></div>`;
document.getElementById('refresh-versions').after(section);
const style = document.createElement('style');
style.textContent = `#deployment-progress{margin:24px 0;padding:20px;border:1px solid #637381;border-radius:12px}#deployment-progress article{padding:12px 0;border-top:1px solid #637381}#deployment-progress code{display:block;overflow-wrap:anywhere}#deployment-progress progress{width:100%}#deployment-progress li{padding:4px 0}#deployment-progress h3{margin:4px 0}`;
document.head.append(style);
const status = section.querySelector('#deployment-status');
const attempts = section.querySelector('#deployment-attempts');
const labels = {queued:'Queued',running:'Running',complete:'Complete',failed:'Needs attention',interrupted:'Stopped reporting',updating:'Updating',current:'Verified'};
const element = (tag, text) => {const node=document.createElement(tag);node.textContent=text;return node;};
let pending=false;
async function refresh() {
  if (pending) return;
  pending=true;
  try {
    const response=await fetch('/api/jswarm-web/deployments',{cache:'no-store',signal:AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error(response.status===404?'Deployment reporting becomes available when this node finishes updating':`HTTP ${response.status}`);
    const report=await response.json();
    const active=report.deployments.filter(row=>['queued','running'].includes(row.status));
    const history=report.deployments.filter(row=>!['queued','running'].includes(row.status)).slice(0,5);
    status.textContent=`${active.filter(row=>row.status==='running').length} active · ${active.filter(row=>row.status==='queued').length} queued · Checked ${new Date(report.checked_at*1000).toLocaleTimeString()}`;
    const fragment=document.createDocumentFragment();
    for (const row of [...active.sort((a,b)=>a.queued_at-b.queued_at),...history]) {
      const article=document.createElement('article');
      article.append(element('h3',`${row.operation} · ${labels[row.status] || 'Unknown'}`));
      if(row.phase)article.append(element('p',row.phase));
      if(row.revision)article.append(element('code',`Target release: ${row.revision}`));
      if(row.nodes.length) {
        const done=row.nodes.filter(node=>node.status==='current').length;
        const meter=document.createElement('progress');meter.max=row.nodes.length;meter.value=done;meter.setAttribute('aria-label','Nodes verified on target release');
        article.append(meter,element('p',`${done} of ${row.nodes.length} nodes verified · ${row.nodes.filter(node=>node.status==='queued').length} waiting`));
        const list=document.createElement('ol');
        for(const node of row.nodes)list.append(element('li',`${node.node} — ${labels[node.status] || 'Unknown'}`));
        article.append(list);
      } else if(row.status==='running') {
        const meter=document.createElement('progress');meter.setAttribute('aria-label',row.phase || 'Deployment in progress');article.append(meter);
      }
      if(row.error)article.append(element('p',`Failure: ${row.error}`));
      article.append(element('small',`${row.finished_at?'Finished':'Queued'} ${new Date((row.finished_at || row.queued_at)*1000).toLocaleString()}`));
      fragment.append(article);
    }
    if(!report.deployments.length)fragment.append(element('p','No deployment attempts recorded yet. New standard deployments appear here automatically.'));
    attempts.replaceChildren(fragment);
  } catch(error) {
    attempts.replaceChildren();
    status.textContent=`Deployment status unavailable: ${error.message}.`;
  } finally {pending=false;}
}
document.getElementById('refresh-versions').addEventListener('click',refresh);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)void refresh();});
setInterval(()=>{if(!document.hidden)void refresh();},2000);
void refresh();
