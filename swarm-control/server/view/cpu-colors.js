// Black-body-inspired utilization ramp; colors indicate load, never temperature.
const stops=[[0,[8,8,12]],[.2,[100,8,4]],[.4,[210,35,4]],[.6,[255,110,8]],[.8,[255,210,65]],[1,[255,250,235]]];
export function cpuColor(percent){
 if(!Number.isFinite(percent))return '#27313a';
 const value=Math.max(0,Math.min(100,percent))/100;
 const upper=stops.findIndex(stop=>stop[0]>=value);
 if(upper<=0)return 'rgb('+stops[0][1].join(',')+')';
 const [low,a]=stops[upper-1],[high,b]=stops[upper],t=(value-low)/(high-low);
 return 'rgb('+a.map((c,i)=>Math.round(c+(b[i]-c)*t)).join(',')+')';
}
