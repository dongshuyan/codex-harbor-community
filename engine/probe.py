"""Bounded Responses probe; only an explicit output-limit result permits expansion."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import UserError, map_request, validated_profile
from proxy import NoRedirect

MAX_REPLY_BYTES = 1024 * 1024
MAX_USAGE = 2**63 - 1  # Native macOS Int must be able to decode the count.
PROBE_TIMEOUT = 60
OUTPUT_LIMITS = (32, 128, 512, 2048, 8192)
SAFE_PARAMS = {'model', 'input', 'stream', 'store', 'tools', 'reasoning', 'reasoning.effort',
               'max_output_tokens', 'previous_response_id', 'temperature', 'top_p',
               'include', 'text', 'parallel_tool_calls', 'service_tier'}


def result(outcome, message, **fields):
    return dict(outcome=outcome, message=message, **fields)


def timeout_result():
    return result('timeout','已达到总时限并停止等待，不会再补测；上游可能仍在处理最后一次请求。')


def http_error(status, raw):
    # Never echo an upstream message/body: providers may include credentials or input.
    param = None
    try:
        error = json.loads(raw).get('error', {})
        value = error.get('param') if isinstance(error, dict) else None
        if isinstance(value, str) and value in SAFE_PARAMS: param = value
    except (ValueError, AttributeError): pass
    if status in (401, 403): message = '认证或权限被拒绝，请检查 API Key 和模型权限。'
    elif status == 404: message = '接口或模型不存在，请检查基础 URL 和实际 ModelName。'
    elif status == 400: message = '上游拒绝请求，请检查模型的 Responses 支持及参数兼容性。'
    elif status == 429: message = '上游限流或额度不足，请检查服务商后台。'
    elif 300 <= status < 400: message = '上游要求重定向；为保护 Key，测活没有跟随跳转。'
    else: message = '上游返回错误，请检查服务商状态。'
    if param: message += f' 上游指出的参数：{param}。'
    return result('http_error', message, http_status=status, error_param=param)


def worker(value):
    profile=value['profile']
    mapping=next(m for m in profile['models'] if m['alias']==value['alias'])
    limit=value.get('max_output_tokens',OUTPUT_LIMITS[0])
    body=map_request(profile, {'model':value['alias'], 'input':'Reply OK.',
                              'stream':False, 'max_output_tokens':limit,
                              'reasoning':{'effort':mapping['effort']}})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request=urllib.request.Request(profile['url']+'/responses', data=json.dumps(body).encode(),
        headers={'Authorization':'Bearer '+value['key'], 'Content-Type':'application/json',
                 'Accept':'application/json', 'Accept-Encoding':'identity',
                 'User-Agent':'Codex-Harbor-Probe/0.5'})
    try:
        with opener.open(request, timeout=value.get('remaining',PROBE_TIMEOUT)) as response:
            raw=response.read(MAX_REPLY_BYTES+1)
            if len(raw)>MAX_REPLY_BYTES:
                return result('invalid_response','响应超过测活读取上限，未认定成功。',http_status=response.status)
            payload=json.loads(raw)
            if not isinstance(payload,dict) or payload.get('object')!='response':
                raise ValueError('Not a Responses object')
            usage=payload.get('usage') or {}
            counts={k:usage.get(k) if isinstance(usage,dict) and type(usage.get(k)) is int
                    and 0 <= usage[k] <= MAX_USAGE else None
                    for k in ['input_tokens','output_tokens']}
            if payload.get('status')=='incomplete':
                detail=payload.get('incomplete_details')
                limited=isinstance(detail,dict) and detail.get('reason')=='max_output_tokens'
                message=(f'已达到 {limit} token 上限，回复未完成；推理也可能占用此限额。' if limited else
                         '服务已响应，但回复未完成。')
                return result('incomplete',message,http_status=response.status,output_limit_reached=limited,**counts)
            output=payload.get('output')
            texts=[c.get('text','') for item in output if isinstance(item,dict) and item.get('type')=='message'
                   for c in item.get('content',[]) if isinstance(c,dict) and c.get('type')=='output_text'
                   and isinstance(c.get('text'),str)] if isinstance(output,list) else []
            text=''.join(texts).strip()
            if payload.get('status')!='completed' or not text:
                return result('invalid_response','HTTP 成功，但没有完整的 Responses 文本回复，未认定测活成功。',
                              http_status=response.status,**counts)
            message='收到完整的 Responses 文本回复。'
            if text!='OK': message+=' 模型未按要求只回复 OK；未显示其原始内容。'
            return result('success',message,
                          http_status=response.status,reply_ok=text=='OK',**counts)
    except urllib.error.HTTPError as error:
        try: return http_error(error.code,error.read(MAX_REPLY_BYTES))
        finally: error.close()
    except TimeoutError: return timeout_result()
    except urllib.error.URLError as error:
        if isinstance(error.reason,TimeoutError): return timeout_result()
        return result('network_error','网络连接或 TLS 错误，没有自动重试。')
    except (ValueError, TypeError, OSError):
        return result('invalid_response','上游返回的内容不是有效的 Responses 回复。')


def attempt(capsule, deadline, cancelled):
    """A worker deadline also bounds DNS, response headers and slow body reads."""
    if cancelled(): return result('cancelled','已取消等待；已发送的请求可能仍在上游处理。')
    remaining=deadline-time.monotonic()
    if remaining<=0:return timeout_result()
    # Key goes through an anonymous pipe, never argv/environment/log files.
    capsule=dict(capsule,remaining=remaining)
    with subprocess.Popen([sys.executable,'-I','-B',str(Path(__file__).resolve()),'--worker'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,
            env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin','LANG':'en_US.UTF-8'}) as proc:
        try:
            payload=json.dumps(capsule)
            while True:
                if cancelled():
                    proc.kill();proc.communicate()
                    return result('cancelled','已取消等待；已发送的请求可能仍在上游处理。')
                remaining=deadline-time.monotonic()
                if remaining<=0:raise subprocess.TimeoutExpired(proc.args,0)
                try:
                    output,_=proc.communicate(payload,timeout=min(.1,remaining))
                    break
                except subprocess.TimeoutExpired:payload=None
            if time.monotonic()>deadline:return timeout_result()
            data=json.loads(output) if proc.returncode==0 else None
            return data if isinstance(data,dict) else result('invalid_response','测活进程未正常返回结果。')
        except subprocess.TimeoutExpired:
            proc.kill();proc.communicate();return timeout_result()
        except ValueError:return result('invalid_response','测活进程未返回有效结果。')


def probe_api(store, ident, alias, *, timeout=PROBE_TIMEOUT, revision=None, cancelled=lambda: False):
    with store.probe_guard(ident,alias):
        path=store.secret_path(ident)
        if path.is_symlink():raise UserError('配置路径不安全。')
        value=json.loads(path.read_text())
        if revision is not None and value.get('revision',0)!=revision:
            raise UserError('API 配置在测活前已改变，请刷新后重新确认测活范围。')
        profile=validated_profile(value['profile'])
        if profile['id']!=ident:raise UserError('配置 ID 不匹配。')
        mapping=next((m for m in profile['models'] if m['alias']==alias),None)
        if profile['kind']!='api' or mapping is None:
            raise UserError('请先保存完整的 API 配置并选择其中一个模型。')
        key=store.secret(ident)
        if not key:raise UserError('请先保存完整的 API 配置。')
        capsule={'profile':profile,'key':key,'alias':alias}
        return run_probe(capsule,timeout=timeout,cancelled=cancelled)


def probe_draft(store, data, alias, *, timeout=PROBE_TIMEOUT, cancelled=lambda:False):
    from model_metadata import draft_connection
    # Validate the draft, but never publish it. Key resolution refuses a changed
    # endpoint unless the user has supplied a Key explicitly in this draft.
    profile=validated_profile(dict(data,name=data.get('name') or '草稿测活'))
    if profile['kind']!='api' or not any(m['alias']==alias for m in profile['models']):
        raise UserError('请选择草稿中的一个完整模型映射。')
    with store.probe_guard(profile['id'],alias):
        url,key=draft_connection(store,data,data.get('api_key',''))
        return run_probe({'profile':profile,'key':key,'alias':alias},timeout=timeout,cancelled=cancelled)



def run_probe(capsule, *, timeout=PROBE_TIMEOUT, cancelled=lambda:False):
    started=time.monotonic();deadline=started+timeout;attempts=[]
    for limit in OUTPUT_LIMITS:
        if cancelled():
            data=result('cancelled','已取消测活；不会再补测，已发送的请求可能仍在上游处理。');break
        if time.monotonic()>=deadline:data=timeout_result();break
        began=time.monotonic()
        data=attempt(dict(capsule,max_output_tokens=limit),deadline,cancelled)
        attempts.append(dict(max_output_tokens=limit,outcome=data['outcome'],
                             elapsed_ms=round((time.monotonic()-began)*1000),
                             **{k:data.get(k) for k in ['http_status','input_tokens','output_tokens']}))
        # No authentication/network/status retry. Only an explicit budget
        # exhaustion can authorize another fresh, history-free short request.
        if data['outcome']!='incomplete' or data.get('output_limit_reached') is not True:break
    else:
        data=dict(data,outcome='budget_exhausted',
                  message=f'已补测至 {OUTPUT_LIMITS[-1]} token 仍未完成回复，已停止；这不等于 API 不可用。')
    for field in ['input_tokens','output_tokens']:
        counts=[a[field] for a in attempts]
        total=sum(counts) if counts and all(type(x) is int for x in counts) else None
        data[field]=total if total is not None and 0<=total<=MAX_USAGE else None
    return dict(data,attempts=attempts,elapsed_ms=round((time.monotonic()-started)*1000),model=next(m['upstream'] for m in capsule['profile']['models'] if m['alias']==capsule['alias']))


if __name__=='__main__':
    try: print(json.dumps(worker(json.load(sys.stdin)),ensure_ascii=False))
    except Exception: print(json.dumps(result('invalid_response','测活未正常完成，没有自动重试。'),ensure_ascii=False))
