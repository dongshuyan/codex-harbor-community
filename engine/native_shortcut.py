"""Native shortcut client. Preparation always goes through the shared protocol."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time


class EntryError(RuntimeError):
    pass


def call(request):
    try:
        result=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).with_name('main.py'))],
                              input=json.dumps(request),text=True,capture_output=True,timeout=25)
        value=json.loads(result.stdout)
        if result.returncode or not value.get('ok'):
            raise EntryError(value.get('error') or '共享启动检查未完成。')
        return value['result']
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        raise EntryError('无法完成共享启动检查；未绕过检查或重复启动。') from exc


def launch(request,*,timeout=145):
    job=call(request);start=time.monotonic()
    ident=job.get('job')
    if not isinstance(ident,str):raise EntryError('启动记录无效，未重复请求。')
    while time.monotonic()-start<timeout:
        if job.get('status') in ('error','exited'):
            raise EntryError(job.get('message') or '本次启动未完成。')
        check=job.get('catalog_readiness') or {}
        if job.get('status')=='running' and check.get('status') in ('verified','unverified'):
            return {'pid':job['pid'],'warning':check.get('message') if check['status']=='unverified' else None}
        time.sleep(.5)
        job=call({'action':'job_status','job':ident})
    raise EntryError('启动结果尚未完整确认。请在 Harbor 核对，未重复启动。')


def main():
    parser=argparse.ArgumentParser(description='使用原生登录与配置，通过共享检查启动 Codex。')
    selection=parser.add_mutually_exclusive_group()
    selection.add_argument('--storage',default='auto')
    selection.add_argument('--history-path')
    args=parser.parse_args()
    request={'action':'native_launch'}
    request.update({'history_path':args.history_path} if args.history_path else {'storage':args.storage})
    try:
        result=launch(request)
        print(result['warning'] or 'Codex 已启动，会话目录已核对。')
        return 0
    except EntryError as exc:
        print(str(exc),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
