"""Private Codex homes plus native Chromium sandbox; shared macOS UI prefs backed up."""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

from core import APP,CODEX,EXTERNAL,Store,UserError,atomic_write,configure,directory,environment,write_json,process_matches,process_stamp
from distribution import discover_app
from recovery import snapshot,lifecycle_lock
from proxy import Gateway
from accounts import account_lock_paths,take_lock


def launch_request(store, profile_id, storage, model=None, *, access_mode='native'):
    if access_mode not in ['native','readonly','workspace']:raise UserError('实例权限模式无效。')
    # Preparation runs inside the worker's lifetime lock, never in the caller.
    with store.registry.guard():
        selection=store.select_storage(storage)
        pinned_path=store.registry.entry(selection)['path']
        store.profile(profile_id)
    job=uuid.uuid4().hex
    jobs=directory(store.root/'jobs')
    job_path=jobs/f'{job}.json'
    write_json(job_path,{'status':'starting','id':profile_id,'storage':selection,'storage_path':pinned_path,'job':job,'created_at':time.time(),'access_mode':access_mode})
    command=[sys.executable,'-I','-B',str(Path(__file__).resolve().with_name('main.py')),
             '--root',str(store.root),'--worker',job,'--profile',profile_id,
             '--storage',selection]
    if model: command.extend(['--model',model])
    if store.external!=EXTERNAL:
        flag='--external' if Path('/Volumes') in store.external.parents else '--test-external'
        command.extend([flag,str(store.external)])
    proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True,
                          env={'PATH':'/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin',
                               'HOME':str(Path.home()),'LANG':'en_US.UTF-8'})
    for _ in range(35):
        time.sleep(.1)
        data=json.loads(job_path.read_text())
        if data['status']!='starting': return data
        if proc.poll() is not None: break
    data=json.loads(job_path.read_text())
    if data['status']=='starting' and proc.poll() is not None:
        data.update(status='error',message='启动进程提前退出，未启动会话。')
        write_json(job_path,data)
    return data


def run_instance(store, job, profile_id, storage, model, *, lab=None):
    jobs=directory(store.root/'jobs');job_path=jobs/f'{job}.json'
    lock_path=store.lock_path(storage,profile_id)
    gateway=None;proc=None;life=None;account_lock=None;backend_switch=None
    state={'id':profile_id,'storage':storage,'job':job,'status':'starting',
           'lifecycle_version':1,'desktop_spawn_attempted':False,'created_at':time.time()}
    with lock_path.open('a') as lock:
        try:
            life=lifecycle_lock(store.root)
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise UserError('这个存储与账号/API 组合已在运行，请使用已有窗口。') from None
            if store.live_desktop(storage,profile_id):
                raise UserError('上次启动的窗口仍在运行；请先在 Harbor 退出对应实例再启动。')
            with store.operation_guard(profile_id),store.registry.guard():
                planned=json.loads(job_path.read_text()) if job_path.exists() else {}
                access_mode=planned.get('access_mode','native')
                if access_mode not in ['native','readonly','workspace']:raise UserError('实例权限模式无效，未启动。')
                state['access_mode']=access_mode
                if planned.get('storage_path',store.registry.entry(storage)['path'])!=store.registry.entry(storage)['path']:
                    raise UserError('会话目录在启动前被修改，请重新选择。')
                created=planned.get('created_at')
                if not isinstance(created,(int,float)) or isinstance(created,bool) or not math.isfinite(created):
                    created=job_path.stat().st_mtime if job_path.exists() else state['created_at']
                state.update(created_at=created,storage_path=store.registry.entry(storage)['path'],
                             worker_pid=os.getpid(),worker_pid_start=process_stamp(os.getpid()))
                if not state['worker_pid_start']:raise UserError('无法核对启动进程身份，未启动会话。')
                # Publish ownership before preparation/snapshot as well as spawn.
                # The caller's timestamp remains stable if this worker dies.
                write_json(job_path,state)
                p=store.profile(profile_id)
                if p['kind']=='official':
                    if any(store.live_desktop(s['id'],profile_id) for s in store.registry.entries()):
                        raise UserError('此官方账号仍有窗口运行，请退出后再换会话位置。')
                    account_path,lease_path=account_lock_paths(store,profile_id)
                    account_lock=take_lock(account_path)
                    backend_switch=take_lock(lease_path)
                    generation=str(directory(store.root/'accounts'/profile_id)/'launch-generation.json')
                    # Fence every old wrapper before the first binding mutation.
                    write_json(Path(generation),{'job':None,'phase':'switching','storage':storage})
                else:
                    lease_path=store.instance_backend_lock(storage,profile_id)
                    backend_switch=take_lock(lease_path)
                    generation=str(directory(store.root/'bindings')/f'{storage}-{profile_id}.json')
                    write_json(Path(generation),{'job':None,'phase':'switching','storage':storage})
                prepared=store.prepare(profile_id,storage,model,launch_job=job)
                prepared['access_mode']=access_mode
            backup=snapshot(store.root)
            # Chromium's Unix singleton socket needs a short private temp path.
            prepared['temporary']=tempfile.mkdtemp(prefix='cs2-',dir='/private/tmp')
            # macOS Unix sockets have a 104-byte path limit. The app builds its
            # IPC socket from CODEX_HOME; keep a private short alias to this NEW home.
            short_home=Path(prepared['temporary'])/'h'
            short_home.symlink_to(prepared['home'],target_is_directory=True)
            prepared['short_home']=str(short_home)
            p=prepared['profile']
            events=directory(store.root/'events')/f'{job}.jsonl'
            def record(event):
                fd=os.open(events,os.O_WRONLY|os.O_APPEND|os.O_CREAT|os.O_NOFOLLOW,0o600)
                with os.fdopen(fd,'a') as handle:handle.write(json.dumps(event)+'\n')
            if p['kind']=='api':
                gateway=Gateway(p,store.secret(profile_id),record)
                gateway.start();configure(prepared,gateway.url,gateway.token)
            else:configure(prepared)
            env=environment(prepared)
            args=[]
            if lab:
                # This branch is available to the isolated acceptance harness only.
                # It writes synthetic auth/settings into the newly prepared test home.
                lab(prepared,env,args)
            wrapper=Path(prepared['temporary'])/'codex-backend'
            # Codex applies its native sandbox to each tool call. An outer
            # Seatbelt profile prevents that sandbox from being initialized.
            # Pin the environment even if desktop shell discovery changes it.
            pinned={k:env[k] for k in ['HOME','ZDOTDIR','CODEX_HOME','CODEX_SQLITE_HOME']}
            # Desktop IPC needs the short alias, but the native sandbox must use
            # the real home. The guard adapts only explicit local permission paths.
            pinned['CODEX_HOME']=prepared['home']
            pinned['HARBOR_DESKTOP_CODEX_HOME_ALIAS']=prepared['short_home']
            lease=str(lease_path)
            write_json(Path(generation),{'job':job,'phase':'ready','storage':storage})
            command=[sys.executable,'-I','-B',str(Path(__file__).with_name('backend_guard.py')),
                     lease,generation,job,str(CODEX)]
            from native_defaults import ROUTE_ENV
            atomic_write(wrapper,'#!/bin/sh\nunset '+' '.join(ROUTE_ENV)+'\n'+''.join('export '+k+'='+shlex.quote(v)+'\n' for k,v in pinned.items())+
                         'exec '+shlex.join(command)+' "$@"\n')
            wrapper.chmod(0o700)
            if backend_switch:backend_switch.close();backend_switch=None
            env['CODEX_CLI_PATH']=str(wrapper)
            binary=discover_app([APP])[2]
            if not binary.is_file():raise UserError('找不到已安装的官方 Codex 应用。')
            state.update(home=prepared['home'],history=prepared['history'],electron=prepared['electron'],
                         backup=backup)
            # Direct spawn avoids reusing another account's Launch Services instance.
            # No secrets are present in argv or environment; Router key stays in this worker.
            cmd=[sys.executable,'-I','-B',str(Path(__file__).with_name('desktop_child.py')),
                 str(job_path),'--user-data-dir='+prepared['electron'],*args]
            log=None
            if lab:log=open(jobs/f'{job}.synthetic.stderr','wb')
            # Persist the attempt first: an interrupted spawn must never look
            # like a proven pre-desktop failure to a future login importer.
            state['desktop_spawn_attempted']=True
            write_json(job_path,state)
            proc=subprocess.Popen(cmd,env=env,cwd=prepared['workspace'],stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL,stderr=log or subprocess.DEVNULL,
                                  start_new_session=True,close_fds=True,
                                  pass_fds=(lock.fileno(),life.fileno())+((account_lock.fileno(),) if account_lock else ()))
            def terminate(_sig,_frame):
                if proc.poll() is None:os.killpg(proc.pid,signal.SIGTERM)
            if __import__('threading').current_thread() is __import__('threading').main_thread():
                signal.signal(signal.SIGTERM,terminate)
                signal.signal(signal.SIGINT,terminate)
            exit_code=proc.wait()
            state.update(json.loads(job_path.read_text()))
            state.update(status='exited' if exit_code==0 else 'error',exit_code=exit_code)
            if exit_code:state['message']='Codex 未正常启动或异常退出；现有版本未受影响。'
            if log:log.close()
        except UserError as e:
            state.update(status='error',message=str(e))
        except Exception as e:
            state.update(status='error',message='启动失败（'+type(e).__name__+'）。配置与旧历史均未被迁移。')
        finally:
            if proc and proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=8)
                except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            if gateway:gateway.close()
            try:
                from organization import Organization
                Organization(store).publish()
            except (OSError,UserError):
                state['warning']='项目组织状态尚未合并；私有窗口原文件已保留，下次启动会再读取。'
            if gateway and gateway.log_failed:state['warning']='API 请求记录写入失败；请求处理未因此停止。'
            write_json(job_path,state)
            if life:life.close()
            if backend_switch:backend_switch.close()
            if account_lock:account_lock.close()
    return state


def stop_instance(store,job_id):
    from core import ID
    if not ID.fullmatch(job_id):raise UserError('实例 ID 无效。')
    path=store.root/'jobs'/f'{job_id}.json'
    if path.is_symlink():raise UserError('实例状态路径不安全。')
    state=json.loads(path.read_text())
    if process_matches(state):
        # Signal only the exact recorded process, never all Codex apps or a PID
        # whose kernel start time no longer matches. Backend exits with its parent.
        try:os.kill(state['pid'],signal.SIGTERM)
        except ProcessLookupError:return {'signal_sent':False,'message':'该实例已经退出。'}
        return {'signal_sent':True,'message':'已要求对应实例退出；等待进程结束后再续聊或修改配置。'}
    return {'signal_sent':False,'message':'该实例已经退出。'}
