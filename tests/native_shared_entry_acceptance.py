"""Real installed desktop through private + controlled-original workers.

All data, credentials and HTTP endpoints are synthetic. The original entry's
user-data-dir/HOME are redirected solely by its in-process lab callback. Real
occupancy is unit/kernel-tested separately; this gate excludes unrelated user
processes because its target is an isolated profile, never the real original.
Project/pin edits are saved fixture edits, not claims of clicking the native UI.
"""
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import tomllib
import uuid
from unittest.mock import patch

import backend_acceptance as lab
import core
import launcher
import native_entry
from organization import Organization,STATE,MAP_KEY
from proxy import Gateway
from native_catalog_acceptance import configure_fixture,catalog,rollouts,logical_history


def main():
    root=lab.ROOT
    report={'status':'running','real_credentials_used':False,'original_history_used':False,
            'scope':'real native desktop/CLI; saved synthetic edits; isolated original user-data-dir'}
    core.write_json(root/'shared-entry-result.json',report)
    server=ThreadingHTTPServer(('127.0.0.1',0),lab.fixture.Mock);server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_port}'
    user=core.directory(root/'user')
    store=core.Store(root/'control',root/'disk',mounted=lambda:False,user_home=user)
    history=core.directory(root/'h')
    rows=store.storage_snapshot();selection='e'*32
    rows['storages'].append({'id':selection,'name':'Synthetic shared history','path':str(history),'enabled':True})
    store.save_storages(rows['storages'],rows['revision'])
    profile=store.save_profile({'kind':'api','name':'Synthetic shared entry','url':base+'/v1',
        'protocol':'responses','api_key':'SYNTHETIC_R','models':[{'alias':'gpt-6-luna',
            'upstream':'synthetic/shared','effort':'low','context':128000,'reasoning':'off'}]})
    prepared=store.prepare(profile['id'],selection);home=Path(prepared['home'])
    gateway=Gateway(profile,'SYNTHETIC_R');gateway.start();core.configure(prepared,gateway.url,gateway.token)
    configure_fixture(home/'config.toml',base)
    ids=[];projects={}
    client=lab.Client(prepared,base)
    try:
        for label in ('a','b'):
            cwd=core.directory(root/('project-'+label))
            projects[label]=client.call('project/create',{'name':'Synthetic '+label,
                'roots':[{'path':str(cwd)}],'idempotencyKey':'legacy-'+label})['project']
            for n in range(2):
                tid=client.start(cwd=str(cwd));assert client.turn(tid,'SYNTHETIC_SHARED_'+label+str(n))
                client.call('thread/name/set',{'threadId':tid,'name':'Synthetic '+label+str(n)})
                ids.append(tid)
    finally:client.stop()
    state={'local-projects':{ 'legacy-'+k:{'id':'legacy-'+k,'name':'Synthetic '+k,
                  'rootPaths':[str(root/('project-'+k))],'createdAt':1000,'updatedAt':1000} for k in projects},
           'project-order':['legacy-a','legacy-b'],'pinned-project-ids':['legacy-a'],
           'thread-project-assignments':{tid:{'projectKind':'local','projectId':'legacy-'+('a' if i<2 else 'b')} for i,tid in enumerate(ids)},
           'sidebar-project-thread-orders':{'legacy-a':{'threadIds':ids[:2],'sortKey':'created_at'},'legacy-b':{'threadIds':ids[2:]}}}
    core.write_json(history/STATE,state)
    # The native worker does not generate/change credentials or config. Seed
    # explicitly fake original-profile files before taking preservation hashes.
    for name in ('config.toml','auth.json','smart2-models.json'):
        shutil.copyfile(home/name,history/name)
    configure_fixture(history/'config.toml',base)
    protected={n:(history/n).read_bytes() for n in ('config.toml','auth.json')}
    for name,data in protected.items():
        (core.directory(root/'before')/name).write_bytes(data)
    before=logical_history(history);bodies=rollouts(history)
    helper=root/'HarborLaunch'
    subprocess.run(['/usr/bin/swiftc','-module-cache-path',str(root/'swift-cache'),
                    str(Path(__file__).resolve().parents[1]/'native/Sources/HarborLaunch/main.swift'),
                    '-o',str(helper)],check=True,capture_output=True,timeout=90)
    def private_lab(prepared,env,args):
        configure_fixture(Path(prepared['home'])/'config.toml',base)
        env.update(CODEX_SPARKLE_ENABLED='false',CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH='1',
                   CODEX_REFRESH_TOKEN_URL_OVERRIDE=base+'/blocked-refresh',CODEX_REVOKE_TOKEN_URL_OVERRIDE=base+'/blocked-revoke')
    def original_lab(history,env,args):
        report['prelaunch_config_auth_unchanged']=all((history/n).read_bytes()==b for n,b in protected.items())
        assert report['prelaunch_config_auth_unchanged']
        env.update(HOME=str(user),CODEX_SPARKLE_ENABLED='false',CODEX_ELECTRON_SKIP_COMPUTER_USE_CANONICAL_REFRESH='1',
                   CODEX_REFRESH_TOKEN_URL_OVERRIDE=base+'/blocked-refresh',CODEX_REVOKE_TOKEN_URL_OVERRIDE=base+'/blocked-revoke')
        args.append('--user-data-dir='+str(root/'original-ui'))
    def phase(name,native=False):
        job=uuid.uuid4().hex;path=core.directory(store.root/'jobs')/(job+'.json')
        core.write_json(path,{'job':job,'created_at':time.time(),'status':'starting','access_mode':'native',
                             'storage_path':str(history)})
        target=native_entry.run if native else launcher.run_instance
        args=(store,job,selection) if native else (store,job,profile['id'],selection,None)
        worker=threading.Thread(target=target,args=args,kwargs={'lab':original_lab if native else private_lab})
        start=time.monotonic();worker.start()
        samples=0;normalized_removals=[]
        try:
            while time.monotonic()-start<140:
                current=json.loads(path.read_text())
                if current.get('status')=='error':raise AssertionError(current.get('message'))
                output=history if native else Path(current['home']) if current.get('home') else None
                # The original fixture exists before the new projection. Start
                # sampling only after the launch journal confirms preparation.
                if current.get('desktop_spawn_attempted') and output and (output/STATE).exists():
                    observed=json.loads((output/STATE).read_text());samples+=1
                    if set(observed.get('local-projects',{}))!={'legacy-a','legacy-b'}:
                        normalized_removals.append('projects')
                    if set(observed.get('thread-project-assignments',{}))!=set(ids):
                        normalized_removals.append('membership')
                    if observed.get('pinned-project-ids')!=(['legacy-b'] if native else ['legacy-a']):
                        normalized_removals.append('pins')
                check=store.root/'startup-checks'/(job+'.json')
                checked=json.loads(check.read_text()) if check.exists() else {}
                if checked.get('status')=='unverified':raise AssertionError(checked)
                if checked.get('status')=='verified':
                    output=history if native else Path(current['home'])
                    value=json.loads((output/STATE).read_text())
                    assert set(catalog(output))==set(ids)
                    assert len(value['local-projects'])==2
                    assert value['sidebar-project-thread-orders']==state['sidebar-project-thread-orders']
                    assert value['pinned-project-ids']==(['legacy-b'] if native else ['legacy-a'])
                    with closing(sqlite3.connect((history/'state_5.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
                        assert db.execute('SELECT count(*) FROM projects').fetchone()[0]==2
                    report[name]={'seconds':round(time.monotonic()-start,3),'threads':len(ids),
                                  'pins':value['pinned-project-ids'],'projects':2,'hydration_samples':samples,
                                  'unexpected_removals':sorted(set(normalized_removals))}
                    assert not normalized_removals,'Native hydration removed projected metadata'
                    if native:
                        worker.join(timeout=5);assert not worker.is_alive(),'Original worker must finish its bounded check'
                    return output
                time.sleep(.2)
            raise AssertionError('Synthetic entry did not become verified in time')
        finally:
            launcher.stop_instance(store,job);worker.join(timeout=25)
            deadline=time.monotonic()+20
            while core.process_matches(json.loads(path.read_text())) and time.monotonic()<deadline:time.sleep(.1)
            assert not core.process_matches(json.loads(path.read_text())),'Owned synthetic desktop did not stop'
            assert not worker.is_alive(),'Owned synthetic worker did not stop'
    print('EVIDENCE='+str(root),flush=True)
    try:
        with patch.object(launcher,'LAUNCH_HELPER',helper),patch.object(launcher,'snapshot',return_value='synthetic-no-shared-preferences'), \
             patch('native_profile.require_idle'):
            first=phase('private')
            value=json.loads((first/STATE).read_text());value['pinned-project-ids']=['legacy-b'];value['project-order']=['legacy-b','legacy-a']
            core.write_json(first/STATE,value)
            original=phase('controlled_original',True)
            original_value=json.loads((original/STATE).read_text());assert original_value['project-order']==['legacy-b','legacy-a']
            # Old private writer saves unrelated state: it must not undo the
            # original entry's later explicit unpin/pin selection.
            original_value['pinned-project-ids']=['legacy-a'];core.write_json(original/STATE,original_value)
            value['synthetic-window-width']=900;core.write_json(first/STATE,value)
            final=phase('private_after_original')
            with closing(sqlite3.connect(final/'sqlite/codex-dev.db')) as db,db:
                db.executemany("DELETE FROM local_thread_catalog WHERE host_id='local' AND thread_id=?",[(x,) for x in ids[:2]])
            assert len(catalog(final))==2
            phase('private_after_catalog_gap')
            report['source_threads_unchanged']=logical_history(history)['threads']==before['threads']
            report['rollout_bytes_unchanged']=rollouts(history)==bodies
            report['source_auth_unchanged']=(history/'auth.json').read_bytes()==protected['auth.json']
            # A fresh native desktop itself adds bundled-plugin/MCP defaults.
            # Attribute that separately from Harbor's pre-launch projection;
            # still require every preexisting synthetic setting to survive.
            old=tomllib.loads(protected['config.toml'].decode())
            new=tomllib.loads((history/'config.toml').read_text())
            def leaves(value,prefix=()):
                result={}
                for key,item in value.items():
                    if isinstance(item,dict):result.update(leaves(item,(*prefix,key)))
                    else:result[(*prefix,key)]=item
                return result
            old_leaves,new_leaves=leaves(old),leaves(new)
            report['preexisting_config_values_preserved']=all(new_leaves.get(k)==v for k,v in old_leaves.items())
            report['native_added_config_fields']=['.'.join(k) for k in new_leaves.keys()-old_leaves.keys()]
            assert all(report[k] for k in ('source_threads_unchanged','rollout_bytes_unchanged','source_auth_unchanged',
                                          'preexisting_config_values_preserved','prelaunch_config_auth_unchanged'))
            report['status']='passed'
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__,message=str(exc));raise
    finally:
        core.write_json(root/'shared-entry-result.json',report)
        gateway.close();server.shutdown();server.server_close();helper.unlink(missing_ok=True)
        print(json.dumps(report),flush=True)


if __name__=='__main__':main()
