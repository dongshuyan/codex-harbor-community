#!/usr/bin/env python3
"""JSON-on-stdin interface for the native launcher. Output never contains API keys."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from core import DEFAULT_ROOT,Store,UserError,model_options,process_matches,ID,directory,atomic_write
from launcher import launch_request,run_instance,stop_instance


def status(store):
    import math
    jobs=[]
    for f in (store.root/'jobs').glob('*.json'):
        if f.is_symlink():continue
        try:
            j=json.loads(f.read_text())
            worker={'pid':j.get('worker_pid'),'pid_start':j.get('worker_pid_start')}
            if j.get('status')=='starting':
                created=j.get('created_at')
                if not isinstance(created,(int,float)) or isinstance(created,bool) or not math.isfinite(created):
                    created=f.stat().st_mtime
                if worker['pid_start'] and not process_matches(worker):
                    j.update(status='error',message='启动进程已退出，未完成启动，请重新启动。')
                elif time.time()-created>120:
                    j.update(status='error',message='启动未完成，请检查后重新启动。')
            if j.get('status')=='running':
                if not process_matches(j):j['status']='exited'
                elif not worker['pid_start']:
                    j['message']='窗口仍在运行，但旧记录无法核对监护进程身份。请先退出此实例再重开。'
                elif not process_matches(worker):
                    j['message']='窗口仍在运行，但监护进程已退出。请先退出此实例再重开。'
            jobs.append(j)
        except (OSError,ValueError):continue
    storage=store.storage_snapshot()
    import profile_order
    profiles=store.profiles()
    from login import snapshot as login_snapshot
    return {'profiles':profiles,'profile_order_revision':profile_order.revision(profiles),'removed_profiles':store.removed_profiles(),'models':model_options(),
            'storages':storage['storages'],'storage_revision':storage['revision'],
            'removed_storages':storage['removed_storages'],
            'accounts':{p['id']:login_snapshot(store,p['id']) for p in store.profiles() if p['kind']=='official'},
            'busy_profiles':[p['id'] for p in store.profiles() if store.running_profile(p['id'])],
            'local_root':str(store.root/'stores/local'),'external_root':str(store.external),
            'jobs':jobs[-100:],'protocols':['responses']}


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default=str(DEFAULT_ROOT))
    external=parser.add_mutually_exclusive_group()
    external.add_argument('--test-external')
    external.add_argument('--external')
    parser.add_argument('--worker');parser.add_argument('--profile');parser.add_argument('--storage');parser.add_argument('--model')
    parser.add_argument('--restore-macos',action='store_true')
    args=parser.parse_args()
    try:
        if args.test_external:
            if Path(args.root).resolve()==DEFAULT_ROOT.resolve(): raise UserError('测试目录参数不能用于正式 Harbor 数据。')
            if Path('/Volumes') in Path(args.test_external).resolve().parents:
                raise UserError('真实移动盘必须检查挂载身份，不能使用测试目录参数。')
            store=Store(Path(args.root),Path(args.test_external),mounted=lambda:True)
        elif args.external:
            from storage import volume_identity
            from core import VOLUME_UUID
            ext=Path(args.external)
            store=Store(Path(args.root),ext,mounted=lambda:volume_identity(ext)==VOLUME_UUID)
        else: store=Store(Path(args.root))
        if args.restore_macos:
            from recovery import restore
            print(json.dumps(restore(store.root),ensure_ascii=False));return
        if args.worker:
            run_instance(store,args.worker,args.profile,args.storage,args.model);return
        data=json.loads(sys.stdin.read() or '{"action":"status"}')
        action=data.get('action')
        if action=='status':result=status(store)
        elif action=='save':result=store.save_profile(data['profile'])
        elif action=='remove':result=store.remove_profile(data['id'],data['revision'])
        elif action=='restore':result=store.restore_profile(data['id'],data['revision'])
        elif action=='purge':result=store.purge_profile(data['id'],data['revision'])
        elif action=='save_profile_order':
            import profile_order
            result=profile_order.save(store,data['ids'],data['revision'])
        elif action=='discover_models':
            from model_metadata import draft_connection, discover
            url,key=draft_connection(store,data['profile'],data.get('api_key',''))
            result=discover(url,key,data['models'])
        elif action=='save_storages':result=store.save_storages(data['storages'],data['revision'])
        elif action in ['remove_storage','restore_storage','purge_storage']:
            result=getattr(store.registry,action.removesuffix('_storage'))(data['id'],data['revision'])
        elif action=='initialize_storage':
            if not isinstance(data.get('revision'),int) or not isinstance(data.get('path'),str):
                raise UserError('目录确认信息缺失，请刷新后重新确认建立目录。')
            result=store.initialize_storage(data['id'],revision=data['revision'],path=data['path'])
        elif action in ['login_start','login_cancel']:
            import login
            result=(login.start if action=='login_start' else login.cancel)(store,data['id'])
        elif action=='quota_read':
            from quota import fetch
            result=fetch(store,data['id'])
        elif action=='cancel_probe':
            token=data.get('probe_id','')
            if not ID.fullmatch(token):raise UserError('测活标识无效。')
            atomic_write(directory(store.root/'probe-control')/(token+'.cancel'),'cancel\n')
            result={'message':'已请求取消测活。'}
        elif action in ('probe','probe_draft'):
            from probe import probe_api,probe_draft
            token=data.get('probe_id')
            if token is not None and not ID.fullmatch(token):raise UserError('测活标识无效。')
            marker=directory(store.root/'probe-control')/(token+'.cancel') if token else None
            try:
                cancelled=lambda:bool(marker and marker.exists())
                if action=='probe_draft':result=probe_draft(store,data['profile'],data['model'],cancelled=cancelled)
                else:result=probe_api(store,data['id'],data['model'],revision=data.get('revision'),cancelled=cancelled)
            finally:
                if marker:marker.unlink(missing_ok=True)
        elif action=='launch':result=launch_request(store,data['id'],data['storage'],access_mode=data.get('access_mode','native'))
        elif action=='stop':result=stop_instance(store,data['job'])
        else:raise UserError('未知操作。')
        print(json.dumps({'ok':True,'result':result},ensure_ascii=False))
    except UserError as e:print(json.dumps({'ok':False,'error':str(e)},ensure_ascii=False))
    except Exception as e:print(json.dumps({'ok':False,'error':'操作失败（'+type(e).__name__+'），请检查新目录的权限。'},ensure_ascii=False))


if __name__=='__main__':main()
