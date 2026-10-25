"""Thin controlled original entry, shared by Smart and a native shortcut.

Only local organization and the selected home are prepared. Native credentials,
config.toml, permissions, plugins, transport and the official App are unchanged.
There is no worker left after the bounded startup check.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import pwd
import subprocess
import threading
import time

from core import APP,UserError,directory,process_stamp,write_json
from organization import Organization,OrganizationConflictError,read_object
from recovery import lifecycle_lock
from sidebar_catalog import prepare_catalog
from startup_gate import acquire
from startup_check import run as check_startup
import runtime_payload


def run(store,job,selection,*,lab=None):
    path=directory(store.root/'jobs')/(job+'.json')
    planned=read_object(path) or {}
    state=dict(planned,id='native',storage=selection,job=job,status='starting',
               worker_pid=os.getpid(),worker_pid_start=process_stamp(os.getpid()),
               native_default_profile=True,desktop_spawn_attempted=False,lifecycle_version=3)
    if runtime_payload.runtime_id():state['runtime_id']=runtime_payload.runtime_id()
    spawned=False;proc=None
    try:
        from native_contract import require_supported
        require_supported(APP)
        with ExitStack() as leases:
            leases.enter_context(lifecycle_lock(store.root))
            gate=leases.enter_context(acquire(store,job))
            with store.registry.guard():
                entry=store.registry.entry(selection)
                if planned.get('storage_path')!=entry['path']:
                    raise UserError('会话位置已改变，请重新启动。')
                store.select_storage(selection)
                history=store.registry.history_path(entry)
                org=Organization(store)
                org.prepare_original(history)
                prepared=prepare_catalog(history,history)
            state.update(home=str(history),history=str(history),catalog_check=prepared,
                         startup_check_version=1,created_at=planned.get('created_at',time.time()))
            environment={'CODEX_HOME':str(history),'CODEX_SQLITE_HOME':str(history)}
            arguments=[]
            if lab:lab(history,environment,arguments)
            state.update(desktop_launch_phase='pending',desktop_spawn_attempted=True)
            write_json(path,state)
            real_home=pwd.getpwuid(os.getuid()).pw_dir
            from launcher import LAUNCH_HELPER
            proc=subprocess.Popen([str(LAUNCH_HELPER),str(path)],
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                cwd=real_home,env={'HOME':real_home,'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8'},
                close_fds=True,start_new_session=True,pass_fds=runtime_payload.inherited_fds())
            spawned=True
            try:proc.stdin.write(json.dumps({'app':str(APP),'environment':environment,
                                            'arguments':arguments,'launchOnly':True,'activates':lab is None}).encode())
            finally:proc.stdin.close()
            try:result=proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # LaunchServices cannot cancel a submitted request. Leave the
                # helper as the sole journal writer for any late callback.
                state=read_object(path) or state
                if state.get('desktop_launch_phase')!='running':
                    write_json(directory(store.root/'startup-checks')/(job+'.json'),
                               {'status':'unverified','message':'系统尚未确认启动结果；已保留请求，未重复启动。'})
                    return state
                result=0
            state=read_object(path) or state
            if result or state.get('desktop_launch_phase')!='running':
                # An unresolved system intent remains visible to the next gate.
                raise UserError(state.get('message') or '系统未确认原生窗口启动，未重复请求。')
            gate.close()
            check_startup(path,directory(store.root/'startup-checks')/(job+'.json'),threading.Event())
    except (OSError,UserError) as exc:
        if spawned:state=read_object(path) or state
        state.update(status='error',message=str(exc) if isinstance(exc,UserError) else '原生入口启动检查未完成；原始数据保留。')
        if isinstance(exc,OrganizationConflictError):state['error_code']='organization_conflict'
        if proc is not None and proc.poll() is None:
            write_json(directory(store.root/'startup-checks')/(job+'.json'),
                       {'status':'unverified','message':state['message']})
        else:write_json(path,state)
    return state
