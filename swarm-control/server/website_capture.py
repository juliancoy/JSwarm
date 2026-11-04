#!/usr/bin/env python3
"""Fresh unauthenticated, read-only synthetic browser observations; stdout is JSON only."""
import datetime
import hashlib
from pathlib import Path
import json
import re
import time
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

SITES = ('https://codecollective.us/', 'https://lifetech.fyi/')


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme+'://'+parsed.hostname+((':'+str(parsed.port)) if parsed.port else '') if parsed.scheme in ('https','http') and parsed.hostname else None


def capture(browser, url):
    context = browser.new_context(viewport={'width':1440,'height':1000},service_workers='block',accept_downloads=False)
    page = context.new_page()
    records, indexed = [], {}
    if hasattr(page, 'route_web_socket'):
        page.route_web_socket('**/*', lambda ws:ws.close())
    errors = []
    page.on('pageerror',lambda error:errors.append('javascript_error'))
    page.add_init_script("window.__lcp=null; new PerformanceObserver(list=>{for(const e of list.getEntries())window.__lcp=e.startTime;}).observe({type:'largest-contentful-paint',buffered:true});")

    def request_started(request):
        target = origin(request.url)
        if not target: return
        row = {'origin':target,'method':request.method,'type':request.resource_type,'status':None,'bytes':None,'duration_ms':None,'error':None,'blocked':None}
        records.append(row); indexed[request]=row

    def guard(route):
        request = route.request
        row = indexed.get(request)
        reason = 'non_read_method' if request.method not in ('GET','HEAD','OPTIONS') else 'media_excluded' if request.resource_type=='media' else 'request_limit' if len(records)>400 else None
        if reason:
            if row is not None:row['blocked']=reason
            route.abort()
        else:route.continue_()

    def responded(response):
        row = indexed.get(response.request)
        if row is not None:
            row['status']=response.status
            try: row['server_ip']=((response.server_addr() or {}).get('ipAddress') or '').strip('[]') or None
            except Exception: pass

    def finished(request):
        row=indexed.get(request)
        if row is not None:
            try:
                row['bytes']=request.sizes().get('responseBodySize')
                timing=request.timing
                row['duration_ms']=round(timing['responseEnd'],2) if timing.get('responseEnd',-1)>=0 else None
            except Exception:pass

    def failed(request):
        row=indexed.get(request)
        if row is not None and not row['blocked']:
            code=re.search(r'net::ERR_[A-Z_]+', request.failure or '')
            row['failure_code']=code.group(0) if code else 'unknown_network_error'
            row['cancelled']=row['failure_code']=='net::ERR_ABORTED'
            row['error']=None if row['cancelled'] else 'request_failed'

    page.on('request',request_started); page.on('response',responded)
    page.on('requestfinished',finished); page.on('requestfailed',failed)
    page.route('**/*',guard)
    # CDP records the script initiator's origin, never URLs, query strings or stacks.
    session=context.new_cdp_session(page); session.send('Network.enable')
    initiators=[]
    def initiated(event):
        target=origin(event.get('request',{}).get('url',''))
        info=event.get('initiator',{})
        frames=info.get('stack',{}).get('callFrames',[])
        source=origin(info.get('url','')) or next((origin(f.get('url','')) for f in frames if origin(f.get('url',''))),None)
        if target and source:initiators.append({'source':source,'target':target,'basis':'CDP request script/parser initiator origin'})
    session.on('Network.requestWillBeSent',initiated)
    observed=datetime.datetime.now(datetime.timezone.utc).isoformat()
    failure=None;started=time.monotonic()
    try:
        page.goto(url,wait_until='domcontentloaded',timeout=25000)
        page.wait_for_timeout(5000)
    except Exception as exc:failure=type(exc).__name__
    metrics={}
    try:
        metrics=page.evaluate("""()=>{const n=performance.getEntriesByType('navigation')[0];return {ttfb_ms:n?.responseStart,dom_content_loaded_ms:n?.domContentLoadedEventEnd,load_ms:n?.loadEventEnd||null,fcp_ms:performance.getEntriesByName('first-contentful-paint')[0]?.startTime,lcp_observed_ms:window.__lcp}}""")
    except Exception:pass
    result={'site':url,'observed':observed,'duration_seconds':round(time.monotonic()-started,2),'requests':records,
        'initiators':initiators,'metrics':metrics,'navigation_error':failure,'javascript_errors':len(errors),
        'final_origin':origin(page.url),'evidence':'synthetic_browser','vantage':'compai · fresh desktop Chromium · no login',
        'collector_revision':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16], 'browser_version':browser.version,
        'coverage':'Homepage load plus 5 seconds; media and non-read HTTP methods blocked; service workers and WebSockets disabled; at most 400 allowed requests. No clicks or authenticated sessions. In-flight requests may lack byte/timing data.'}
    # Closing the observation window must not turn in-flight requests into failures.
    page.remove_listener('requestfailed',failed)
    page.remove_listener('requestfinished',finished)
    page.remove_listener('response',responded)
    page.remove_listener('request',request_started)
    context.close()
    return result


if __name__=='__main__':
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path='/usr/bin/google-chrome')
        try: print(json.dumps([capture(browser,url) for url in SITES]))
        finally:browser.close()
