const root=document.querySelector('#test-deployment');
if(root){
 const el=(tag,text)=>{const node=document.createElement(tag);node.textContent=text;return node;};
 root.append(el('h2','Swarm Control for Android'),el('p','Use your PIdP account in your normal browser on any network. The Android app opens the same console.'));
 const download=el('a','Download Android launcher');download.href='/api/v1/test-app/download';download.download='swarm-control-test.apk';
 const signin=el('a','Sign in with PIdP');signin.href='/auth/login';
 const actions=el('div','');actions.className='test-deployment-actions';actions.append(signin,download);root.append(actions,el('small','Downloads require a PIdP session. Android asks you to approve installation.'));
}
