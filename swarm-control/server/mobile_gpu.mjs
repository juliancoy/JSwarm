// Fixed synthetic compute check; never opens the user's browser sessions.
import {chromium} from '@playwright/test';
import {createServer} from 'node:http';
import {readFile,writeFile} from 'node:fs/promises';
import {probeBrowserGPU} from './swarm-gpu-hardware.mjs';
const reportPath='../mobile-artifacts/mobile-report.json';
const server=createServer((req,res)=>{res.setHeader('Content-Type','text/html');res.end('<!doctype html><title>GPU compute check</title>');});
await new Promise(resolve=>server.listen(Number(process.env.MOBILE_SERVER_PORT),'127.0.0.1',resolve));
let browser,page;
try {
 browser=await chromium.connectOverCDP(process.env.PLAYWRIGHT_CDP_URL);
 page=await browser.contexts()[0].newPage();
 await page.goto(`http://localhost:${process.env.MOBILE_SERVER_PORT}/`);
 const gpu=await Promise.race([page.evaluate(probeBrowserGPU),new Promise((_,reject)=>setTimeout(()=>reject(Error('GPU compute check timed out')),15000))]);
 const report=JSON.parse(await readFile(reportPath,'utf8'));report.gpu=gpu;
 if(gpu.status==='passed')report.checks.push({name:'WebGPU compute kernel and readback',status:'passed'});
 await writeFile(reportPath,JSON.stringify(report,null,2));
 console.log(JSON.stringify({kind:'mobile_gpu',gpu}));
 if(gpu.status==='failed')throw Error(gpu.reason);
} finally {if(page)await page.close();if(browser)await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
