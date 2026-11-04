export function renderVisualArtifacts(job) {
 const artifacts=job.artifacts||[];
 if(!artifacts.length)return null;
 const section=document.createElement('section');section.className='visual-artifacts';
 const heading=document.createElement('h4');heading.textContent='Layout renders';section.append(heading);
 const note=document.createElement('p');note.textContent='Synthetic layouts. Review the images; a successful render is not visual approval.';section.append(note);
 const grid=document.createElement('div');grid.className='visual-artifact-grid';
 const list=document.createElement('details'),summary=document.createElement('summary');summary.textContent='All screenshots and reports ('+artifacts.length+')';list.append(summary);
 for(const artifact of artifacts){
  const url=new URL(artifact.url,location.origin);
  if(url.origin!==location.origin||url.pathname!=='/api/distributed-tests/artifacts')continue;
  const link=document.createElement('a');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';link.textContent=artifact.name;
  if((artifact.name.startsWith('contact-')||artifact.url.includes('suite=mobile'))&&artifact.name.endsWith('.png')){
   const image=document.createElement('img');image.src=url.href;image.alt=artifact.name.replaceAll('-',' ').replace('.png','');image.loading='lazy';image.decoding='async';
   const card=document.createElement('figure');card.append(image);const caption=document.createElement('figcaption');caption.append(link);card.append(caption);grid.append(card);
  }else{const row=document.createElement('div');row.append(link);list.append(row);}
 }
 section.append(grid,list);return section;
}
