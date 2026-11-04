export const defaults = {layer:'links',lanOnly:false,allConnections:false,geographic:false,groupProviders:false,labels:false,paused:false,resolveNames:true,repulsion:50};
const storageKey='lan-map.settings.v1';
export function loadSettings(storage) {
  const settings={...defaults};
  try {
    const saved=JSON.parse((storage||localStorage).getItem(storageKey)||'{}');
    for(const name of ['lanOnly','allConnections','geographic','groupProviders','labels','paused','resolveNames'])if(typeof saved?.[name]==='boolean')settings[name]=saved[name];
    if(['links','ip'].includes(saved?.layer))settings.layer=saved.layer;
    if(Number.isFinite(saved?.repulsion))settings.repulsion=Math.max(10,Math.min(100,saved.repulsion));
  }catch{}
  return settings;
}
export function saveSettings(settings,storage) {
  try {(storage||localStorage).setItem(storageKey,JSON.stringify(settings));return true;}catch{return false;}
}
