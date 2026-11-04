export function coordinates(value) {
  return value && Number.isFinite(value.latitude) && Math.abs(value.latitude)<=90 &&
    Number.isFinite(value.longitude) && Math.abs(value.longitude)<=180 ? value : null;
}
export function project(longitude, latitude) { return {x:longitude*4, y:-latitude*4}; }
export function geographicEdge(edge, nodes) {
  return Boolean(nodes.get(edge.a)?.geographic && nodes.get(edge.b)?.geographic);
}
export function geographicNodes(nodes, geography = {}) {
  const locations = geography.locations || {}, groups = new Map();
  const mapped=nodes.map(node => {
    const memberLocations=node.provider_group ? node.members.map(member=>coordinates(locations[member.ip])) : [];
    const sameLocation=memberLocations.length && memberLocations.every(value=>value && value.latitude===memberLocations[0]?.latitude && value.longitude===memberLocations[0]?.longitude);
    const direct = node.provider_group ? (sameLocation ? memberLocations[0] : null) : coordinates(locations[node.ip]);
    const local = Boolean(node.mac || node.self) && !node.external;
    const location = direct || (local ? coordinates(locations.site) : null);
    if (!location) return {...node, geographic:null, mapVisible:false, x:0,y:0,vx:0,vy:0};
    const anchor = project(location.longitude, location.latitude);
    const key = anchor.x+':'+anchor.y;
    const result={...node,mapVisible:true,geographic:{...location,shared_site:!direct,anchor,offset:{x:0,y:0}},...anchor,vx:0,vy:0};
    const group=groups.get(key)||[];group.push(result);groups.set(key,group);
    return result;
  });
  for(const group of groups.values())group.sort((a,b)=>a.id.localeCompare(b.id)).forEach((node,index)=>{
    // Readability offsets are bounded SCREEN pixels, never invented degrees of longitude.
    const radius=index ? Math.min(36,10*Math.sqrt(index)) : 0,angle=index*2.4;
    node.geographic.offset={x:Math.cos(angle)*radius,y:Math.sin(angle)*radius};
  });
  return mapped;
}

// Collision packing affects display pixels only; geographic anchors remain unchanged.
export function separateGeographicNodes(nodes, zoom, radiusFor) {
  const placed=new Map(),grid=new Map(),cell=64;
  let largest=0,rightmost=-Infinity;
  const bin=(x,y)=>Math.floor(x/cell)+':'+Math.floor(y/cell);
  for(const node of nodes.filter(n=>n.geographic).sort((a,b)=>Number(b.perspective)-Number(a.perspective)||a.id.localeCompare(b.id))){
    const radius=radiusFor(node),base={x:node.x*zoom+node.geographic.offset.x,y:node.y*zoom+node.geographic.offset.y};
    const free=point=>{
      const extent=Math.ceil((radius+largest+3)/cell),gx=Math.floor(point.x/cell),gy=Math.floor(point.y/cell);
      for(let x=gx-extent;x<=gx+extent;x++)for(let y=gy-extent;y<=gy+extent;y++)for(const other of grid.get(x+':'+y)||[])
        if(Math.hypot(point.x-other.x,point.y-other.y)<radius+other.radius+3)return false;
      return true;
    };
    let point=base;
    for(let attempt=0;!free(point);attempt++){
      const distance=8*Math.sqrt(attempt+1),angle=(attempt+1)*2.399963229728653;
      point=attempt<8192?{x:base.x+Math.cos(angle)*distance,y:base.y+Math.sin(angle)*distance}:{x:rightmost+radius+4,y:base.y};
    }
    const item={...point,radius};placed.set(node.id,item);
    const key=bin(point.x,point.y),bucket=grid.get(key)||[];bucket.push(item);grid.set(key,bucket);
    largest=Math.max(largest,radius);rightmost=Math.max(rightmost,point.x+radius);
  }
  return placed;
}
