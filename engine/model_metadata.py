"""Read-only model discovery. Provider data never supplies executable configuration."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parent))
from core import UserError, validate_url
from proxy import NoRedirect
from reasoning import LEVELS, FIELDS

PRESET_CONTEXT=128000
MAX_BYTES=8*1024*1024
TIMEOUT=12


def context_number(value):
    if type(value) is int:return value if 4096<=value<=2000000 else None
    if not isinstance(value,str) or len(value)>80:return None
    text=unicodedata.normalize('NFKC',value).strip().lower()
    text=re.sub(r'\s*(tokens?|词元)\s*$','',text)
    text=re.sub(r'[,\s_]','',text)
    match=re.fullmatch(r'(\d+(?:\.\d+)?)([km]?)',text)
    if not match:return None
    try:number=Decimal(match[1])*{'':1,'k':1000,'m':1000000}[match[2]]
    except InvalidOperation:return None
    return int(number) if number==number.to_integral_value() and 4096<=number<=2000000 else None


def parameter_state(mapping):
    """Legacy values are user-owned; reading a profile never migrates its file."""
    state=mapping.get('parameters')
    if state is None:return dict(mode='auto',manual=['context','reasoning'],context_source='saved',reasoning_source='saved',catalog_model='')
    if not isinstance(state,dict) or state.get('mode') not in ('auto','preset'):
        raise UserError('模型参数来源无效。')
    manual=state.get('manual',[])
    if not isinstance(manual,list) or any(x not in ('context','reasoning') for x in manual):
        raise UserError('模型参数的手动修改标记无效。')
    allowed={'preset','router','catalog','manual','saved'}
    result=dict(mode=state['mode'],manual=sorted(set(manual)))
    for field in ('context_source','reasoning_source'):
        source=state.get(field,'preset')
        if source not in allowed:raise UserError('模型参数来源标记无效。')
        result[field]=source
    for field in ('catalog_model','reasoning_catalog_model'):
        reference=state.get(field,'')
        if not isinstance(reference,str) or len(reference)>200 or any(ord(c)<32 for c in reference):
            raise UserError('模型参考名称无效。')
        result[field]=reference
    for field in ('catalog_provider','reasoning_catalog_provider'):
        provider=state.get(field,'')
        if provider not in ('','cherry'):raise UserError('模型参考目录无效。')
        result[field]=provider
    return result


def public_catalog():
    return json.loads((Path(__file__).parent/'context-catalog.json').read_text())['models']


@lru_cache(maxsize=1)
def cherry_catalog():
    return json.loads((Path(__file__).parent/'cherry-catalog.json').read_text())['models']


def reference_match(model, catalog):
    """Match actual IDs, preserving versions, sizes and thinking variants.

    Cherry uses normalization as a fallback too. This narrower subset only
    removes a slash prefix and normalizes numeric version separators; a unique
    match is required. Never borrow capabilities from the Codex display alias.
    """
    if model in catalog:return model,catalog[model]
    def normalized(value):
        return re.sub(r'(?<=\d)[.,_p](?=\d)','-',value.split('/')[-1].lower())
    target=normalized(model)
    matches=[k for k in catalog if normalized(k)==target]
    return (matches[0],catalog[matches[0]]) if len(matches)==1 else ('',{})


def select_record(payload, model):
    """Return {} when absent, None when duplicate records disagree."""
    if not isinstance(payload,dict):return {}
    rows=payload.get('data',payload.get('models',[]))
    if not isinstance(rows,list):return {}
    exact=[r for r in rows if isinstance(r,dict) and r.get('id',r.get('model_name',r.get('slug')))==model]
    # Duplicates with differing capabilities are not a safe automatic default.
    if not exact:return {}
    return exact[0] if all(r==exact[0] for r in exact) else None


def resolve(model,record,catalog,*,cherry=None):
    data=dict(context=PRESET_CONTEXT,context_source='preset',reasoning='off',levels=[],effort='low',
              effort_field='reasoning.effort',effort_values={},reasoning_source='preset',catalog_model='',reasoning_catalog_model='',
              catalog_provider='',reasoning_catalog_provider='')
    # A contradictory provider contract is different from missing metadata.
    # Keep conservative presets instead of enabling public-reference limits.
    if record is None:return data
    reference,entry=reference_match(model,cherry_catalog() if cherry is None else cherry)
    sections=[record]+[record[k] for k in ('model_info','top_provider','limits','limit','capabilities') if isinstance(record.get(k),dict)]
    for section in sections:
        for key in ('context_length','context_window','max_context_window','max_context_tokens','context'):
            number=context_number(section.get(key))
            if number is not None:data.update(context=number,context_source='router');break
        if data['context_source']=='router':break
    if data['context_source']=='preset':
        number=context_number(entry.get('contextWindow'))
        if number is not None:data.update(context=number,context_source='catalog',catalog_model=reference,catalog_provider='cherry')
    if data['context_source']=='preset':
        matches=[model] if model in catalog else [k for k in catalog if k.split('/')[-1]==model.split('/')[-1]]
        if len(matches)==1 and context_number(catalog[matches[0]]) is not None:
            data.update(context=catalog[matches[0]],context_source='catalog',catalog_model=matches[0])
    explicit_reasoning=False
    for section in sections:
        # An explicit unsupported/invalid contract must not be silently replaced
        # by a more permissive public reference.
        if section.get('reasoning') is False or section.get('supports_reasoning') is False:
            explicit_reasoning=True
            data['reasoning_source']='router'
            break
        if any(key in section for key in ('supported_reasoning_efforts','supported_reasoning_levels','reasoning_field')):
            explicit_reasoning=True
        levels=section.get('supported_reasoning_efforts',section.get('supported_reasoning_levels'))
        if isinstance(levels,list):
            levels=[x.get('effort') if isinstance(x,dict) else x for x in levels]
        else:continue
        field=section.get('reasoning_field','reasoning.effort')
        if not levels or any(x not in LEVELS for x in levels) or field not in FIELDS:continue
        levels=[x for x in LEVELS if x in levels]
        values=section.get('effort_values',{x:x for x in levels})
        if not isinstance(values,dict) or any(not isinstance(values.get(x),str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',values[x]) for x in levels):continue
        default=section.get('default_reasoning_effort')
        default=default if default in levels else levels[0]
        standard=field=='reasoning.effort' and all(values[x]==x for x in levels)
        data.update(reasoning='standard' if standard else 'custom',levels=levels,effort=default,
                    effort_field=field,effort_values={x:values[x] for x in levels},reasoning_source='router')
        break
    if not explicit_reasoning:
        # Discrete effort controls can be represented by Responses. A budget or
        # on/off-only control is not an effort ladder, even if Cherry projects
        # synthetic supportedEfforts for its own multi-protocol adapters.
        reference_reasoning=entry.get('reasoning',{})
        controls=reference_reasoning.get('controls',[])
        if controls:
            data.update(reasoning_source='catalog',reasoning_catalog_model=reference,reasoning_catalog_provider='cherry')
        if reference_reasoning.get('wireDialect')=='budget':controls=[]
        for control in controls:
            if control.get('kind')!='effort':continue
            levels=[x for x in LEVELS if x in control.get('values',[])]
            if not levels:continue
            default=control.get('default')
            default=default if default in levels else ('low' if 'low' in levels else levels[0])
            data.update(reasoning='standard',levels=levels,effort=default,
                        effort_values={x:x for x in levels},reasoning_source='catalog',reasoning_catalog_model=reference,reasoning_catalog_provider='cherry')
            break
    return data


def draft_connection(store,profile,key):
    url=validate_url(str(profile.get('url','')))
    key=str(key).strip()
    if key and (len(key)>16384 or any(ord(c)<32 for c in key)):raise UserError('API Key 格式无效。')
    if key and profile.get('key_url')!=url:
        raise UserError('请先确认完整的基础 URL，再输入 Key 或确认向此地址获取信息。')
    ident=profile.get('id')
    if ident:
        with store.operation_guard(ident,shared=True):
            saved=store.public({'id':ident})
            if saved['revision']!=profile.get('revision'):raise UserError('配置已改变，请刷新后重新编辑。')
            if saved['kind']!='api':raise UserError('此来源不是 API。')
            if not key:
                if url!=saved['url']:raise UserError('基础 URL 已改变。请重新填写 Key，才会向新地址获取信息或测活。')
                key=store.secret(ident)
    if not key:raise UserError('请先填写 API Key。')
    return url,key


def fetch_worker(value):
    catalog=public_catalog()
    base=value['url'];status='ok';http_status=None;payload={}
    request=urllib.request.Request(base+'/models',headers={'Authorization':'Bearer '+value['key'],
        'Accept':'application/json','Accept-Encoding':'identity','User-Agent':'Codex-Harbor-Metadata/0.6'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(request,timeout=TIMEOUT) as response:
            http_status=response.status;raw=response.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES:raise ValueError('bounded')
            payload=json.loads(raw)
            if not isinstance(payload,dict) or not isinstance(payload.get('data',payload.get('models')),list):raise ValueError('shape')
    except urllib.error.HTTPError as error:
        status='http_error';http_status=error.code;error.close()
    except TimeoutError:status='timeout'
    except urllib.error.URLError:status='network_error'
    except (ValueError,OSError):status='invalid_response'
    models={}
    for model in value['models']:
        record=select_record(payload,model)
        if record is None:status='conflicting_metadata'
        resolved=resolve(model,record,catalog)
        # Even allowlisted metadata may echo a short credential. Never expose it
        # as a custom reasoning value in the bridge or editor.
        if value['key'] and any(value['key'] in v for v in resolved['effort_values'].values()):
            for field in ('reasoning','levels','effort','effort_field','effort_values','reasoning_source','reasoning_catalog_model','reasoning_catalog_provider'):
                resolved[field]=resolve(model,{},catalog)[field]
        models[model]=resolved
    return {'status':status,'http_status':http_status,'models':models}


def discover(url,key,models,*,timeout=TIMEOUT):
    url=validate_url(url)
    if not isinstance(models,list) or not 1<=len(models)<=64 or any(not isinstance(x,str) or not x.strip() or len(x)>200 or any(ord(c)<32 for c in x) for x in models):
        raise UserError('请填写实际 ModelName 后再获取参数。')
    capsule=dict(url=url,key=key,models=list(dict.fromkeys(models)))
    with subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve())],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,
            env={'PATH':'/usr/bin:/bin:/opt/homebrew/bin','LANG':'en_US.UTF-8'}) as process:
        try:
            output,_=process.communicate(json.dumps(capsule),timeout=timeout)
            value=json.loads(output) if process.returncode==0 else None
            if not isinstance(value,dict):raise ValueError('worker')
            return value
        except (subprocess.TimeoutExpired,ValueError):
            timed=process.poll() is None
            if timed:process.kill()
            process.communicate()
            return {'status':'timeout' if timed else 'invalid_response','http_status':None,
                    'models':{m:resolve(m,{},public_catalog()) for m in models}}


if __name__=='__main__':
    try:print(json.dumps(fetch_worker(json.load(sys.stdin)),ensure_ascii=False))
    except Exception:sys.exit(1)
