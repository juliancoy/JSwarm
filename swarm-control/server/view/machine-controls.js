export const machineActions=[['pause','Pause','Pause admission of new jobs; running jobs continue.'],['update','Update','Install the published JSwarm release on this machine.'],['resume','Resume','Restore the previous worker policy; automatic mode respects battery power.'],['auto','Auto','Use the automatic, battery-aware worker policy.']];

export function machineStateTags(state){
 if(!state||state.connection!=='live')return [{label:state?.connection==='loading'?'Loading node state':'Node state '+(state?.connection||'unavailable'),tone:'unknown'}];
 const tags=[];
 const updating=['starting','updating'].includes(state.update_phase)||state.lifecycle==='updating';
 if(updating)tags.push({label:'Updating',tone:'busy'});
 else if(state.accepting_jobs===true)tags.push({label:'Accepting jobs',tone:'ready'});
 else if(state.accepting_jobs===false)tags.push({label:state.policy==='maintenance'?'Maintenance':'Paused',tone:'paused',title:state.reason});
 if(state.policy)tags.push({label:'Policy · '+({auto:'Automatic',always:'Always on',paused:'Paused',draining:'Replacing node',maintenance:'Maintenance'}[state.policy]||state.policy),tone:'policy'});
 if(state.on_battery===true)tags.push({label:'On battery',tone:'paused'});
 if(state.update_failure)tags.push({label:'Update rolled back',tone:'paused'});
 return tags;
}

export function actionDisabled(action,state,pending=false){
 if(pending||!state||state.connection!=='live'||['sending','sent'].includes(state.action?.status))return true;
 if(action==='pause')return state.policy==='paused';
 if(action==='auto')return state.policy==='auto';
 if(action==='update')return ['starting','updating'].includes(state.update_phase)||state.lifecycle==='updating';
 return false;
}
