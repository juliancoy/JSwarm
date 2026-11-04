"""Hierarchical, evidence-only runtime estimates for the submitted work queue."""
import math
import json
import statistics
import time
from collections import defaultdict


def positive(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def signature(item):
    runtime=item.get('runtime') or {}
    r=item.get('execution') or {}
    sink='gpu' if runtime.get('gpu_used') or r.get('sink')=='gpu' or str(item.get('worker','')).endswith('#gpu') else 'cpu'
    # Keep reference generations and execution backends separate; presentation
    # labels and memory reservations are not identities of scientific work.
    return (item.get('run_type'),item.get('kind'),item.get('generation') or item.get('parent'),sink)


def quantile(values,p):
    values=sorted(values);pos=(len(values)-1)*p
    lo=int(pos);hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(pos-lo)


def attach_memory_profiles(items):
    """Profile verified cgroup peaks; never pool different execution settings."""
    keys=('schema','machine','image','reference_key','reference_layout','backend','batch_bases','sort_memory_mb','cpus')
    def identity(item):
        context=item.get('profiling_context')
        if not isinstance(context,dict) or context.get('schema')!='alignment-memory.v1':return None
        if any(context.get(key) is None for key in keys):return None
        return (str(item.get('worker') or '').split('#')[0],json.dumps({k:context[k] for k in keys},sort_keys=True))
    groups=defaultdict(list);failed=set()
    for item in items:
        key=identity(item)
        if key is None:continue
        if item.get('status')=='failed' or item.get('queue_state')=='failed' or item.get('configuration_equivalent') is False:failed.add(key)
        if item.get('status')=='completed' and item.get('output_verified') is True and positive(item.get('peak_memory_bytes')):
            groups[key].append(item)
    count=0
    for item in items:
        key=identity(item);context=item.get('profiling_context') or {}
        samples=groups.get(key,[])
        # Memory does not scale linearly with reads. Do not extrapolate beyond
        # an observed input envelope or use tiny benchmark samples for full jobs.
        samples=[s for s in samples if positive(s['profiling_context'].get('pairs')) and positive(s['profiling_context'].get('input_bytes'))]
        if not samples:continue
        observations=len(samples);high_water=max(s['peak_memory_bytes']/1024**2 for s in samples);independent={}
        for sample in samples:
            fingerprint=sample['profiling_context'].get('input_fingerprint')
            if not isinstance(fingerprint,str) or len(fingerprint)!=64:continue
            if fingerprint not in independent or sample['peak_memory_bytes']>independent[fingerprint]['peak_memory_bytes']:
                independent[fingerprint]=sample
        # Repeated experiments on the same shard are useful observations, but
        # must not masquerade as five independent production inputs.
        distinct=len(independent)
        if independent:samples=list(independent.values())
        max_pairs=max(s['profiling_context']['pairs'] for s in samples)
        max_bytes=max(s['profiling_context']['input_bytes'] for s in samples)
        covered=positive(context.get('pairs')) and positive(context.get('input_bytes')) and context['pairs']<=max_pairs and context['input_bytes']<=max_bytes
        peaks=[s['peak_memory_bytes']/1024**2 for s in samples]
        scratch=max(s['profiling_context'].get('scratch_mb',0) for s in samples)
        recommended=64*math.ceil(max(high_water*1.5+192,scratch+384)/64)
        qualified=distinct>=5 and covered and key not in failed and context.get('baseline_configuration') is True
        item['memory_profile']={'samples':len(samples),'observations':observations,'independent_inputs':distinct,'median_peak_mb':statistics.median(peaks),
            'p95_peak_mb':quantile(peaks,.95),'max_peak_mb':high_water,'recommended_limit_mb':recommended if qualified else None,
            'qualified':qualified,'blocked_by_failed_attempt':key in failed,'input_envelope_covered':bool(covered),
            'max_pairs':max_pairs,'max_input_bytes':max_bytes,'shared_cache_separate':context.get('reference_layout')=='node-shared-ram',
            'basis':'Verified completed cgroup peaks; exact machine, image, reference, backend, CPU count, batch and sort settings',
            'margin':'50% above observed maximum plus 192 MiB; at least scratch capacity plus 384 MiB; rounded up to 64 MiB',
            'policy':'At least five distinct verified inputs within the observed envelope; recommendation only, no automatic parameter changes or correctness qualification'}
        count+=1
    return {'profiled_jobs':count,'configuration_groups':len(groups)}


def attach_estimates(items,now=None):
    now=time.time() if now is None else now
    memory=attach_memory_profiles(items)
    groups=defaultdict(list)
    for item in items:
        elapsed=(item.get('runtime') or {}).get('elapsed_seconds')
        if item.get('status')=='completed' and positive(elapsed) and not item.get('superseded_by') and item.get('run_type') in ('mapping','genetic-qc','web'):
            groups[signature(item)].append(item)
    validation=[]
    for group in groups.values():
        history=[]
        for sample in sorted(group,key=lambda s:s.get('completed_at') or s.get('updated',0)):
            prior=[s for s in history if s.get('worker')==sample.get('worker')]
            if len(prior)>=3:
                predicted=statistics.median(s['runtime']['elapsed_seconds'] for s in prior[-30:])
                validation.append(abs(predicted-sample['runtime']['elapsed_seconds'])/sample['runtime']['elapsed_seconds'])
            history.append(sample)
    coverage=0
    for item in items:
        if item.get('status') not in ('running','preparing','waiting_capacity','queued','accepted'):continue
        candidates=groups.get(signature(item),[])
        same=[s for s in candidates if s.get('worker')==item.get('worker')]
        exact=[s for s in same if (s.get('execution') or {}).get('resources')==(item.get('execution') or {}).get('resources') and s.get('reference_mode')==item.get('reference_mode')]
        samples,basis=(exact,'same machine and configuration') if len(exact)>=3 else (same,'same machine; configuration may differ') if same else (candidates,'fleet fallback; machine may differ')
        samples=sorted(samples,key=lambda s:s.get('completed_at') or s.get('updated',0),reverse=True)[:100]
        if not samples:continue
        unit=item.get('work_units');normalized=positive(unit) and all(positive(s.get('work_units')) for s in samples)
        values=[s['runtime']['elapsed_seconds']*(unit/s['work_units'] if normalized else 1) for s in samples]
        elapsed=(item.get('runtime') or {}).get('elapsed_seconds') if item.get('status')=='running' else 0
        elapsed=elapsed if positive(elapsed) else 0
        survivors=[v-elapsed for v in values if v>elapsed]
        overrun=elapsed>=statistics.median(values)
        # A censored running task cannot finish in the past. Use conditional
        # survivors, or a clearly identified tail extrapolation when exhausted.
        extrapolated=not survivors
        remaining=survivors or [max(1,quantile(values,.9)*.25)]
        estimate={'typical_seconds':statistics.median(values),'remaining_seconds':statistics.median(remaining),
                  'remaining_low_seconds':quantile(remaining,.1),'remaining_high_seconds':quantile(remaining,.9),
                  'samples':len(samples),'basis':basis,'normalized':bool(normalized),'overrun':overrun,
                  'extrapolated':extrapolated,'confidence':'low' if len(samples)<5 or basis.startswith('fleet') or overrun else 'medium',
                  'sampled_at':now,'range_kind':'empirical 10–90% sample range; not a confidence interval'}
        item['runtime_estimate']=estimate;coverage+=1
    return {'estimated_jobs':coverage,'timed_samples':sum(map(len,groups.values())),'memory':memory,
            'validation':{'method':'chronological same-machine median backtest; only earlier completions',
                          'predictions':len(validation),'median_absolute_percentage_error':statistics.median(validation) if validation else None}}
