"""Bounded, per-launch readiness check. It never monitors or syncs open windows."""
from pathlib import Path
import json
import subprocess
import sys
import time

if __name__=='__main__':sys.path.insert(0,str(Path(__file__).resolve().parent))

from core import UserError,process_matches,write_json
from organization import read_object
from sidebar_catalog import verify_catalog


def check_once(job,cancel,timeout,*,command=None):
    """A slow mount can block a read; supervise a disposable read-only child."""
    command=command or [sys.executable,'-I','-B',str(Path(__file__).resolve()),'--once',job['home'],job['history']]
    proc=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL,close_fds=True,
                          env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'})
    deadline=time.monotonic()+timeout
    try:
        while not cancel.is_set() and time.monotonic()<deadline:
            try:output,_=proc.communicate(timeout=min(.25,max(.001,deadline-time.monotonic())))
            except subprocess.TimeoutExpired:continue
            if proc.returncode:raise UserError('目录核对进程未完成；未修改原始历史。')
            value=json.loads(output)
            if not isinstance(value,dict) or value.get('status') not in ('verified','unverified','loading'):
                raise UserError('目录核对结果无效；未修改原始历史。')
            return value
        return {'status':'unverified','message':'目录读取未在限定时间完成；未修改原始历史。'}
    finally:
        if proc.poll() is None:
            proc.kill()
            try:proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:pass  # An OS-stalled reader owns no write capability or lease.


def run(job_path,result_path,cancel,*,timeout=120,interval=2):
    start=time.monotonic()
    result={'status':'loading','message':'窗口启动后正在核对会话目录。'}
    def save():
        result['elapsed_seconds']=round(time.monotonic()-start,3)
        write_json(result_path,result)
    try:
        save()
        while not cancel.is_set() and time.monotonic()-start<timeout:
            job=read_object(Path(job_path)) or {}
            if job.get('status') in ('error','exited') or (job.get('status')=='running' and not process_matches(job)):
                result.update(status='cancelled',message='窗口已退出，目录核对已结束。');save();return
            if job.get('status')=='running':
                result=check_once(job,cancel,max(.001,timeout-(time.monotonic()-start)))
                observed=result.pop('_receipt',None)
                if cancel.is_set():break
                if time.monotonic()-start>=timeout:break
                if result['status']=='verified' and observed is not None:
                    write_json(Path(job['home'])/'.harbor-catalog-receipt.json',observed)
                save()
                if result['status'] in ('verified','unverified'):return
            cancel.wait(interval)
        result.update(status='cancelled' if cancel.is_set() else 'unverified',
                      message='目录核对已中止。' if cancel.is_set() else '会话目录尚未核对完成；窗口可继续使用，请勿把暂时空白视为历史丢失。')
        save()
    except (OSError,ValueError,KeyError,UserError,subprocess.SubprocessError):
        # A separate result file cannot race the native launch helper's journal.
        # If even it cannot be written, status() reports the missing check.
        try:
            result.update(status='unverified',message='无法完成会话目录核对；原始历史未修改。');save()
        except OSError:pass


if __name__=='__main__':
    if len(sys.argv)!=4 or sys.argv[1]!='--once':raise SystemExit(2)
    print(json.dumps(verify_catalog(sys.argv[2],sys.argv[3],write_receipt=False)))
