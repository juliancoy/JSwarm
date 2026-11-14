// One same-origin page directory for map.local and every local node frontend.
const pages = [
  ['pipeline.html', 'Pipeline', 'Visual job progress'],
  ['tasks.html', 'Tasks', 'Job list and execution details'],
  ['index.html', 'Network map', 'Network topology and devices'],
  ['swarm-status.html', 'Swarm status', 'Machine and connection summaries'],
  ['tests.html', 'Tests', 'Distributed test runs and results'],
  ['monitor.html', 'Resource monitor', 'Detailed live node resources'],
  ['task-manager.html', 'Task Manager', 'Resources alongside task activity'],
  ['versions.html', 'Node versions', 'Reported releases on connected computers'],
  ['workers.html', 'Worker controls', 'Pause and resume compute'],
  ['genome-explorer.html', 'Genome Explorer', 'Genome datasets and results'],
  ['research.html', 'Research', 'DNA analysis jobs'],
  ['history.html', 'History', 'Recorded swarm activity'],
  ['codex.html', 'CompAI', 'Assistant and app deployment'],
  ['swarm-control.html', 'Swarm Control', 'App deployment'],
  ['websites.html', 'Website analysis', 'Opens OrgPortal statistics'],
  ['join-node.html', 'Join a node', 'Approve a new machine'],
];

if (!document.querySelector('swarm-navigation')) {
  const host = document.createElement('swarm-navigation');
  const root = host.attachShadow({mode: 'open'});
  const style = document.createElement('style');
  style.textContent = `
    :host{display:block;position:relative;z-index:1000;font:14px/1.4 system-ui,sans-serif;color:#ecf1f5}
    nav{display:flex;align-items:center;gap:8px;padding:8px 20px;background:#111c25;border-bottom:1px solid #344651}
    a{color:inherit;text-decoration:none;border-radius:5px;padding:7px 10px;white-space:nowrap}
    a:hover,summary:hover{background:#263b48}a[aria-current=page]{background:#254d48;color:#b9f6dc}
    a:focus-visible,summary:focus-visible{outline:2px solid #82d8bf;outline-offset:2px}
    .brand{font-size:12px;font-weight:750;letter-spacing:.12em;margin-right:auto}
    details{position:relative}summary{cursor:pointer;list-style:none;padding:7px 10px;border:1px solid #526772;border-radius:5px;white-space:nowrap}
    summary::-webkit-details-marker{display:none}summary::after{content:' ▾'}
    .pages{position:absolute;right:0;top:calc(100% + 8px);display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;width:min(540px,calc(100vw - 32px));max-height:70vh;overflow:auto;box-sizing:border-box;padding:10px;background:#111c25;border:1px solid #526772;border-radius:8px;box-shadow:0 10px 35px #0007}
    .pages a{white-space:normal}.pages small{display:block;font-size:12px;color:#a7bbc7;font-weight:400;margin-top:3px}
    @media(max-width:600px){nav{padding:7px 12px;gap:2px}.shortcut{display:none}.pages{grid-template-columns:1fr}}
    @media(prefers-color-scheme:light){:host{color:#182536}nav,.pages{background:#f4f7fb;border-color:#b5c4cf}a:hover,summary:hover{background:#e0e9ef}a[aria-current=page]{background:#d1ede3;color:#145840}.pages small{color:#526177}}
  `;
  const nav = document.createElement('nav');
  nav.setAttribute('aria-label', 'Swarm pages');
  const current = location.pathname === '/' ? 'pipeline.html' : location.pathname.split('/').pop().replace(/^(pipeline|tasks|monitor|tests|workers|research|history|swarm-status|task-manager|codex|swarm-control|join-node)$/, '$1.html');
  function link(page, label, description) {
    const a = document.createElement('a');
    a.href = '/' + page;
    a.textContent = label;
    if (current === page) a.setAttribute('aria-current', 'page');
    if (description) {
      const small = document.createElement('small');
      small.textContent = description;
      a.append(small);
    }
    return a;
  }
  const brand = link('pipeline.html', 'SWARM');
  brand.className = 'brand';
  nav.append(brand);
  for (const page of ['pipeline.html', 'index.html', 'tests.html', 'monitor.html']) {
    const item = pages.find(row => row[0] === page);
    const a = link(...item.slice(0, 2));
    a.className = 'shortcut';
    nav.append(a);
  }
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = 'All pages';
  const directory = document.createElement('div');
  directory.className = 'pages';
  directory.append(...pages.map(row => link(...row)));
  details.append(summary, directory);
  nav.append(details);
  root.append(style, nav);
  document.body.prepend(host);
  root.addEventListener('keydown', event => {
    if (event.key === 'Escape') { details.open = false; summary.focus(); }
  });
  document.addEventListener('click', event => {
    if (!event.composedPath().includes(host)) details.open = false;
  });
}
