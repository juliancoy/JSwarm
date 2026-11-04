const stages = [
 {name:'Raw reads',status:'Located',type:'input',files:['FASTQ / FASTQ.GZ'],icon:'≋',summary:'Sequenced fragments with a quality score for every base.',input:'Sequencing output',output:'Reads for alignment',tool:'Read quality and alignment tools',evidence:'Raw reads were inventoried on the data disk. Full read-quality analysis has not been established.'},
 {name:'Alignments',status:'Basic checks passed',type:'input',files:['BAM','BAI'],icon:'☷',summary:'Reads placed against a reference genome. BAI indexes support regional access.',input:'FASTQ + FASTA reference',output:'Aligned BAM + index',tool:'samtools',evidence:'Two existing BAM files passed header/end-of-file checks. This does not establish whole-genome coverage. FASTA (.fa / .fasta) holds the reference; .fai indexes it.'},
 {name:'Variant calls',status:'Prepared',type:'done',files:['gVCF','VCF.GZ','TBI / CSI'],icon:'⋮',summary:'Variant calls describe differences from the reference; gVCF can also retain reference-confidence blocks.',input:'Existing normalized GRCh38.p13 variants',output:'Indexed called-variant VCF',tool:'bcftools',evidence:'Prepared called variants, trimming unused alleles and excluding reference-only blocks. Indexes enable region lookups. No silent PASS, depth, or quality cutoff was applied.'},
 {name:'QC + audit',status:'Verified baseline',type:'done',files:['VCF + CSQ','JSON','TXT'],icon:'✓',summary:'Summarize called variants and audit the functional annotations already present.',input:'Called VCF + existing VEP-annotated VCF',output:'QC statistics + annotation summaries',tool:'bcftools + private CPU batch runner',evidence:'214 batches across six computers matched the local baseline. Existing VEP annotations were audited; VEP was not rerun. JSON stores structured summaries; TXT stores tool statistics. Variant-only QC does not measure genome-wide callability.'},
 {name:'Coverage + VEP',status:'Connect for status',type:'next',files:['BAM + BAI','FASTA','VCF + CSQ'],icon:'◉',summary:'Measure whole-genome coverage and depth, audit reference confidence, and produce reproducible offline functional annotation.',input:'Alignments + reference + versioned VEP cache',output:'Coverage reports + newly annotated VCF',tool:'samtools + offline Ensembl VEP',evidence:'Connect to read the latest coverage and fresh annotation status. The versioned offline pipeline uses VEP 115.2 and a release-115 GRCh38 cache. Private variants remain offline.'},
 {name:'Compare',status:'Planned',type:'future',files:['VCF','BED','PGEN / PVAR / PSAM'],icon:'⇄',summary:'Harmonize reference datasets before comparing variation across genomes.',input:'Validated, assembly-matched variants + cohort metadata',output:'Descriptive research summaries',tool:'PLINK 2 + R / Bioconductor',evidence:'Public downloads have started separately. BED can describe benchmark genomic intervals; PLINK 2 uses PGEN/PVAR/PSAM for cohort data. Statistical population analysis requires an appropriate reference cohort.'}
];
const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text)node.textContent=text;if(cls)node.className=cls;return node;};
const list=document.querySelector('#pipeline');
let selectedStage=3;
function select(index){
 selectedStage=index;
 list.querySelectorAll('button').forEach((b,i)=>b.setAttribute('aria-pressed',String(i===index)));
 const s=stages[index],detail=document.querySelector('#detail');
 const heading=el('div',null,'detail-heading');heading.append(el('span',s.icon,'stage-icon'),el('h3',s.name),el('span',s.status,'badge'));
 const flow=el('div',null,'file-flow');flow.append(el('p',s.input),el('span','→'),el('strong',s.tool),el('span','→'),el('p',s.output));
 detail.replaceChildren(heading,el('p',s.summary),flow,el('p',s.evidence,'evidence'));
}
stages.forEach((s,i)=>{const item=el('li',null,s.type),button=el('button');button.type='button';button.setAttribute('aria-controls','detail');button.append(el('span',String(i+1).padStart(2,'0'),'number'),el('span',s.icon,'stage-icon'),el('strong',s.name),el('small',s.status));const types=el('span',null,'filetypes');s.files.forEach(f=>types.append(el('code',f)));button.append(types);button.onclick=()=>select(i);item.append(button);list.append(item);});
select(3);
const comparison=document.querySelector('#comparisons');
function comparisons(rows){comparison.replaceChildren(...rows.map(r=>{const card=el('article');card.append(el('h3',r.name),el('p',r.status));return card;}));}
comparisons(['Watson','Venter','HG001','HG002','HG005'].map(name=>({name,status:'Status requires a connected session'})));
async function refresh(){
 const message=document.querySelector('#live-message'),button=document.querySelector('#refresh'),metrics=document.querySelector('#metrics');button.disabled=true;
 try{
  const response=await fetch('/api/v1/swarm/research',{cache:'no-store'});
  if(!response.ok)throw new Error(response.status===401?'Sign in to Swarm Control to read private execution progress.':'Execution status unavailable. Historical milestone above remains dated Oct 25.');
  const data=await response.json();comparisons(data.public_downloads);const run=data.run;
  if(data.followup){
   const names={bam_coverage:'BAM coverage',gvcf_span_audit:'gVCF span audit',fresh_offline_vep:'Fresh offline VEP'};
   if(data.followup.jobs.depth_distribution)names.depth_distribution='Whole-genome depth distribution';
   stages[4].status=data.followup.status==='passed'?'Completed':data.followup.status==='failed'?'Needs review':'In progress';
   const completed=data.followup.status==='passed';
   document.querySelector('#current-eyebrow').textContent='LIVE FOLLOW-UP';
   document.querySelector('#current-title').textContent=completed?'Coverage + fresh annotation complete':'Coverage + fresh annotation: '+stages[4].status.toLowerCase();
   document.querySelector('#current-description').textContent=completed?'Whole-BAM coverage, depth distribution, reference-confidence audit and fresh offline annotation have completed. Validation receipts and genomic results remain private.':'Read the live operational status below. Individual genomic results remain private.';
   document.querySelector('#current-badge').textContent=completed?'Analysis complete':'Live follow-up';
   document.querySelector('#current-state').textContent=stages[4].status;
   list.children[4].className=completed?'done':'next';
   stages[4].evidence=Object.entries(names).map(([key,label])=>`${label}: ${data.followup.jobs[key]?.status||'unknown'}`).join(' · ')+'. Reports remain private. Coverage and reference-confidence summaries do not establish clinical callability.';
   list.children[4].querySelector('small').textContent=stages[4].status;
   if(selectedStage===4)select(4);
  }
  metrics.hidden=!run;
  if(run){metrics.replaceChildren(...[[`${run.completed} / ${run.total}`,'batches completed'],[String(run.workers),'workers with completed batches'],[run.verified?'Verified':'Unconfirmed','output verification']].map(([value,label])=>{const card=el('div');card.append(el('strong',value),el('span',label));return card;}));message.textContent=`Latest recorded run: ${run.status} · ${run.failed} failed batches · report updated ${new Date(run.updated_at).toLocaleString()}. Checked ${new Date(data.observed_at).toLocaleTimeString()}.`;}
  else message.textContent='No readable run state on this controller. The dated milestone above is historical evidence.';
 }catch(error){metrics.hidden=true;message.replaceChildren(document.createTextNode(error.message+' '));comparisons(['Watson','Venter','HG001','HG002','HG005'].map(name=>({name,status:'Live status unavailable'})));const link=el('a','Open Swarm Control');link.href='/';message.append(link);}
 finally{button.disabled=false;}
}
document.querySelector('#refresh').onclick=refresh;refresh();
setInterval(()=>{if(!document.hidden)refresh();},30000);


const jobFeedback=document.querySelector('#job-feedback'),jobList=document.querySelector('#job-list'),profile=document.querySelector('#job-profile'),startJob=document.querySelector('#start-job');
let loadingJobs=false,submitting=false,submission=null;
async function jobApi(body){
 const response=await fetch('/api/v1/swarm/research/jobs',{cache:'no-store',...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
 if(!response.ok)throw new Error(response.status===401?'Sign in to Swarm Control to manage jobs.':'Job request unavailable. Retry with the same request ID.');
 return response.json();
}
async function loadJobs(){
 if(loadingJobs)return;loadingJobs=true;
 try{
  const data=await jobApi();const selected=profile.value;
  profile.replaceChildren(...data.profiles.map(name=>{const option=el('option',name);option.value=name;return option;}));
  if(data.profiles.includes(selected))profile.value=selected;
  profile.disabled=!data.profiles.length;startJob.disabled=submitting||!data.profiles.length;
  jobFeedback.textContent=data.profiles.length?'Queue another analysis while other jobs run.':'No owner-configured DNA analysis profile available.';
  jobList.replaceChildren(...data.jobs.map(job=>{
   const row=el('article',null,'job-row'),text=el('div');text.append(el('strong',job.kind==='research'?'DNA analysis':'Distributed tests'),el('p',`${job.status} · ${new Date(job.submitted_at).toLocaleString()}`));row.append(text);
   if(job.progress)text.append(el('p',`${job.progress.completed} / ${job.progress.total} generated batches complete · ${job.progress.failed} failed`));
   if(job.run_id)text.append(el('small',job.run_id));
   if(['queued','running'].includes(job.status)){
    const cancel=el('button','Cancel');cancel.type='button';cancel.onclick=async()=>{cancel.disabled=true;try{await jobApi({cancel:job.id});await loadJobs();}catch(error){jobFeedback.textContent=error.message;cancel.disabled=false;}};row.append(cancel);
   }
   return row;
  }));
 }catch(error){jobFeedback.textContent=error.message;profile.disabled=true;startJob.disabled=true;jobList.replaceChildren();}
 finally{loadingJobs=false;}
}
document.querySelector('#job-form').onsubmit=async event=>{
 event.preventDefault();if(submitting)return;submitting=true;startJob.disabled=true;
 if(!submission||submission.profile!==profile.value)submission={profile:profile.value,request_id:crypto.randomUUID()};
 try{await jobApi(submission);submission=null;await loadJobs();jobFeedback.textContent='Analysis queued. Workers will claim batches as resources become available.';}
 catch(error){jobFeedback.textContent=error.message;}
 finally{submitting=false;startJob.disabled=profile.disabled;}
};
loadJobs();setInterval(()=>{if(!document.hidden&&!submitting)loadJobs();},5000);
