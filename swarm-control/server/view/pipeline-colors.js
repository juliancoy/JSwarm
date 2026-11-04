import {deviceLevels,deviceColor} from './levels-model.js';
export const computerName=worker=>String(worker||'').split('#')[0].toLowerCase();
// CPU/RAM legends identify a computer by the first matching network device.
// First identity wins for machines with both Wi-Fi and Ethernet addresses.
export function computerColors(network,metrics={hosts:[]}){const colors=new Map();for(const row of deviceLevels(network,metrics,'cpu')){const host=computerName(row.telemetryHost||row.name);if(host&&!colors.has(host))colors.set(host,deviceColor(row.deviceId||row.id));}return colors;}
