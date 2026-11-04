export type Ticket = {aud: 'archive-peers-v1' | 'launcher-enroll-v1' | 'launcher-session-v1' | 'launcher-admin-v1' | 'launcher-backup-v1'; room: string; peer: string; iat: number; exp: number};
export const ROOM = /^[a-f0-9]{64}$/;
const encoder = new TextEncoder();

function decode(value: string): Uint8Array<ArrayBuffer> {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error('Invalid token');
  return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
}

export async function verifyTicket(token: string, secret: string, time = Date.now(), audiences: string[] = ['archive-peers-v1','launcher-session-v1']): Promise<Ticket> {
  if (!secret || token.length > 2048) throw new Error('Invalid token');
  const parts = token.split('.');
  if (parts.length !== 3) throw new Error('Invalid token');
  const [header, body, signature] = parts;
  const algorithm = JSON.parse(new TextDecoder().decode(decode(header)));
  if (algorithm.alg !== 'HS256' || algorithm.typ !== 'JWT') throw new Error('Invalid token');
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), {name:'HMAC', hash:'SHA-256'}, false, ['verify']);
  if (!await crypto.subtle.verify('HMAC', key, decode(signature), encoder.encode(header+'.'+body))) throw new Error('Invalid token');
  const ticket = JSON.parse(new TextDecoder().decode(decode(body))) as Ticket;
  const now = Math.floor(time/1000);
  if (!audiences.includes(ticket.aud) || !ROOM.test(ticket.room) ||
      !/^[a-f0-9-]{36}$/.test(ticket.peer) || !Number.isSafeInteger(ticket.exp) || !Number.isSafeInteger(ticket.iat) ||
      ticket.iat > now+30 || ticket.exp <= now || ticket.exp <= ticket.iat || ticket.exp-ticket.iat > 3600) throw new Error('Invalid token');
  return ticket;
}

export async function issueTicket(claims: Ticket, secret: string): Promise<string> {
  const encode = (value: unknown) => btoa(JSON.stringify(value)).replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_');
  const body = encode({alg:'HS256',typ:'JWT'})+'.'+encode(claims);
  const key = await crypto.subtle.importKey('raw',encoder.encode(secret),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  const signature = new Uint8Array(await crypto.subtle.sign('HMAC',key,encoder.encode(body)));
  return body+'.'+btoa(String.fromCharCode(...signature)).replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_');
}
export async function credentialHash(value: string): Promise<string> {
  if (!/^[a-f0-9]{64}$/.test(value)) throw new Error('Invalid credential');
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(value))),x=>x.toString(16).padStart(2,'0')).join('');
}
