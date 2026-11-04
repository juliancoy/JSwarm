export function breadcrumbNav(label){const nav=document.createElement('nav');nav.className='breadcrumbs';nav.setAttribute('aria-label',label);return nav;}
export function setBreadcrumbs(nav,items){
 const signature=items.map(item=>item.label).join('\0');if(nav.dataset.path===signature)return;nav.dataset.path=signature;
 const list=document.createElement('ol');
 items.forEach((item,index)=>{const li=document.createElement('li'),current=index===items.length-1;let node;
  if(item.href){node=document.createElement('a');node.href=item.href;}
  else if(item.action){node=document.createElement('button');node.type='button';node.onclick=item.action;}
  else node=document.createElement('span');
  node.textContent=item.label;if(current)node.setAttribute('aria-current','location');li.append(node);list.append(li);
 });
 nav.replaceChildren(list);nav.classList.remove('breadcrumb-flash');void nav.offsetWidth;nav.classList.add('breadcrumb-flash');
}
const page=breadcrumbNav('Page and test run');page.id='page-breadcrumbs';document.querySelector('header')?.after(page);
const base=()=>[{label:'Network',href:'/'},...(location.pathname.includes('tests')?[{label:'Application tests',href:'/tests.html'}]:[])];
setBreadcrumbs(page,base());
document.addEventListener('test-context',event=>{const {run,host}=event.detail;const items=base();if(run)items.push({label:(run.finished?'Run ':'Live run ')+new Date(run.started).toLocaleTimeString()});if(host)items.push({label:host});setBreadcrumbs(page,items);});
