"""Executable v1.1 relay contract. No live credentials or network."""
import contextlib
import copy
import io
import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_inbox import hooks
from agent_inbox.cli import main
from agent_inbox.cloud_protocol import canonical, strict_loads, validate_envelope
from agent_inbox.cloudsync import (CloudClient, CloudSyncError, SyncEngine, SyncWorker,
    endpoint_origin, login, map_project, replay, reset_generation, save_config, state,
    sync_status, validate_pull, LOCAL_WARNING)
from agent_inbox.db import get_connection, init_db
from agent_inbox.service import InboxService

TIME = '2020-01-01T00:00:00.000Z'
MESSAGE = dict(envelope_version=1, message_id='eml_remote',
    thread=dict(thread_id='thr_remote', root_message_id='eml_remote', home_project='remote', subject='Across machines', created_at=TIME),
    sender='alice@remote', sender_session='s-remote', recipients={'to':['bob@local'], 'cc':['eve@third']},
    subject='Across machines', body_markdown='Hello', reply_to_email_id=None, references=[], sent_at=TIME, origin_device='dev_remote')


def page(envelopes=(), cursor=0, generation='gen_a', more=False, user='usr_a'):
    items = [dict(seq=seq, received_at=TIME, envelope=e) for seq,e in envelopes]
    return dict(protocol_version=1,user_id=user,generation=generation,messages=items,
                last_seq=items[-1]['seq'] if items else cursor,has_more=more)


def child(parent, mid='eml_child', sent='2019-01-01T00:00:00.000Z'):
    e = copy.deepcopy(parent)
    e.update(message_id=mid,reply_to_email_id=parent['message_id'],references=parent['references']+[parent['message_id']],sent_at=sent)
    return e


class CloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config = self.path / 'config' / 'cloud.json'
        p = mock.patch('agent_inbox.cloudsync.config_path',return_value=self.config)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {'AGENT_INBOX_DB':str(self.path/'db')})
        p.start(); self.addCleanup(p.stop)
        self.conn = get_connection()
        self.addCleanup(self.conn.close)
        self.conn.execute("UPDATE cloud_state SET endpoint='https://example.com',user_id='usr_a',generation='gen_a',auth_state='authenticated'")
        map_project(self.conn, 'https://example.com/owner/local.git', 'local')
        self.service = InboxService(self.conn)
        self.http = mock.Mock(url='https://example.com')
        self.http.pull.side_effect = lambda after_seq,generation=None,limit=200: page(cursor=after_seq,generation=generation)
        self.http.push.side_effect = lambda messages,generation: dict(protocol_version=1,user_id='usr_a',generation=generation,
            results=[dict(message_id=e['message_id'],status='accepted',seq=i+1) for i,e in enumerate(messages)])
        self.engine = SyncEngine(self.http,'dev_local')

    def send(self, token='local', body='body'):
        return self.service.send_email('bob@local',['alice@remote'],[],'Local',body,token)['email_id']

    def status(self, mid):
        return dict(self.conn.execute('SELECT * FROM cloud_envelopes WHERE message_id=?',(mid,)).fetchone())

    def pull(self, items, **kwargs):
        self.http.pull.side_effect = None
        self.http.pull.return_value = page(items, **kwargs)
        self.engine.sync_once(self.conn)

    def clear_backoff(self):
        self.conn.execute('UPDATE cloud_state SET push_retry_at=0,pull_retry_at=0')
        self.conn.execute('UPDATE cloud_envelopes SET retry_at=0')

    def test_exact_immutable_envelope_and_topology(self):
        root = self.send()
        reply = self.service.reply_email(root,'alice@remote','reply','reply')['email_id']
        self.conn.execute('UPDATE emails SET delivery_id=100 WHERE id=?',(root,))
        self.engine.sync_once(self.conn)
        sent = self.http.push.call_args.args[0]
        self.assertEqual([e['message_id'] for e in sent],[root,reply])
        self.assertEqual(sent[1]['thread'],sent[0]['thread'])
        self.assertEqual(sent[1]['references'],[root])
        self.assertEqual(sent[1]['reply_to_email_id'],root)
        self.assertEqual(set(sent[0]),set(MESSAGE))
        payload = self.status(root)['envelope_json']
        self.assertEqual(payload,validate_envelope(sent[0]))
        self.assertEqual(self.status(root)['state'],'acknowledged')
        reset_generation(self.conn,'gen_b')
        self.engine.device_id = 'other-device'
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(root)['envelope_json'],payload)
        self.assertEqual(state(self.conn)['last_pulled_seq'],0)

    def test_bad_acknowledgments_never_mark_synced(self):
        mid=self.send()
        good=dict(protocol_version=1,user_id='usr_a',generation='gen_a',results=[dict(message_id=mid,status='accepted',seq=1)])
        bad=[{},dict(good,results=[]),dict(good,user_id='other'),dict(good,generation='wrong')]
        for field,value in [('message_id','eml_other'),('status','ok'),('seq',True),('seq',0),('seq',9007199254740992)]:
            ack=copy.deepcopy(good); ack['results'][0][field]=value; bad.append(ack)
        self.http.push.side_effect=None
        for response in bad:
            self.clear_backoff(); self.http.push.return_value=response
            self.engine.sync_once(self.conn)
            self.assertNotEqual(self.status(mid)['state'],'acknowledged')
            self.assertIsNone(self.conn.execute('SELECT cloud_synced_at FROM emails').fetchone()[0])

    def test_lost_ack_duplicate_and_captured_ids(self):
        first=self.send()
        def response(messages,generation):
            self.send('concurrent')
            return dict(protocol_version=1,user_id='usr_a',generation=generation,results=[dict(message_id=first,status='duplicate',seq=3)])
        self.http.push.side_effect=response
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(first)['ack_seq'],3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM emails WHERE cloud_synced_at IS NULL').fetchone()[0],1)

    def test_poison_neighbors_and_blocked_children(self):
        bad=self.send('bad', 'x'*65537)
        dependent=self.service.reply_email(bad,'alice@remote','reply','child')['email_id']
        good=self.send('good')
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(bad)['state'],'permanently_rejected')
        self.assertEqual(self.status(dependent)['reason'],'blocked_by_ancestor')
        self.assertEqual(self.status(good)['state'],'acknowledged')
        count=self.http.push.call_count
        self.engine.sync_once(self.conn)
        self.assertEqual(self.http.push.call_count,count)

    def test_named_atomic_conflict_retries_neighbors(self):
        bad=self.send('bad'); good=self.send('good')
        regular=self.http.push.side_effect
        def push(messages,generation):
            if any(e['message_id']==bad for e in messages):
                raise CloudSyncError('conflict',code='payload_conflict',ids=[bad])
            return regular(messages,generation)
        self.http.push.side_effect=push
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(bad)['state'],'permanently_rejected')
        self.assertEqual(self.status(good)['state'],'acknowledged')

    def test_pull_independent_of_quota_push_and_retry_after(self):
        mid=self.send()
        self.http.push.side_effect=CloudSyncError('quota',code='user_quota_exceeded',retry_after=300)
        self.pull([(7,MESSAGE)])
        self.assertEqual(self.status(mid)['state'],'retryable')
        self.assertGreaterEqual(state(self.conn)['push_retry_at'],time.time()+299)
        self.assertEqual(state(self.conn)['last_pulled_seq'],7)
        self.assertEqual(self.service.list_threads('bob@local',unread_only=True)[0]['thread_id'],'thr_remote')
        self.http.pull.return_value=page(cursor=7)
        self.engine.sync_once(self.conn)
        self.assertEqual(self.http.pull.call_count,2)
        self.assertEqual(self.http.push.call_count,1)

    def test_push_independent_of_pull_failure(self):
        mid=self.send()
        self.http.pull.side_effect=OSError('offline')
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(mid)['state'],'acknowledged')

    def test_401_suspends_both_and_exposes_status(self):
        self.send()
        self.http.pull.side_effect=CloudSyncError('auth',code='reauth_required')
        self.engine.sync_once(self.conn); self.engine.sync_once(self.conn)
        self.http.push.assert_not_called()
        self.assertEqual(self.http.pull.call_count,1)
        self.assertEqual(sync_status(self.conn)['auth_state'],'reauth-required')

    def test_page_atomic_ancestry_and_token_conflicts(self):
        bad=child(MESSAGE); bad['reply_to_email_id']='eml_absent'; bad['references']=['eml_remote','eml_absent']
        self.pull([(1,MESSAGE),(2,bad)])
        self.assertEqual(state(self.conn)['last_pulled_seq'],0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM emails').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT value FROM delivery_counter').fetchone()[0],0)
        self.clear_backoff()
        local=self.send()
        self.conn.execute('UPDATE emails SET client_token=? WHERE id=?',('cloud-import:v1:eml_remote',local))
        self.pull([(1,MESSAGE)])
        self.assertEqual(state(self.conn)['last_pulled_seq'],0)
        self.assertIn('token conflict',state(self.conn)['last_error'])

    def test_pull_projection_reply_order_read_replay_and_hooks(self):
        self.send()
        with mock.patch('agent_inbox.hooks.get_connection',side_effect=lambda: get_connection(self.path/'db')):
            self.assertIsNotNone(hooks.build_notice('alice@remote',self.service.list_threads('alice@remote',unread_only=True),now=1000))
            old=child(MESSAGE)
            old['recipients']['to']=['alice@remote','bob@local']
            self.pull([(3,MESSAGE),(7,old)])
            threads=self.service.list_threads('alice@remote',unread_only=True)
            self.assertEqual(threads[0]['thread_id'],'thr_remote')
            self.assertIsNotNone(hooks.build_notice('alice@remote',threads,now=1001))
            self.assertIsNone(hooks.build_notice('alice@remote',threads,now=1002))
        thread=self.service.get_thread('bob@local','thr_remote')
        self.assertEqual([e['email_id'] for e in thread['emails']],['eml_child','eml_remote'])
        projection=self.conn.execute("SELECT * FROM threads WHERE id='thr_remote'").fetchone()
        self.assertEqual(projection['created_at'],TIME)
        self.assertEqual(projection['last_email_at'],TIME)
        joined=self.conn.execute("SELECT MIN(joined_at) FROM thread_inboxes WHERE thread_id='thr_remote'").fetchone()[0]
        self.assertEqual(joined,old['sent_at'])
        self.service.mark_thread_read('bob@local','thr_remote')
        delivery=self.conn.execute('SELECT value FROM delivery_counter').fetchone()[0]
        replay(self.conn)
        self.clear_backoff()
        self.pull([(3,MESSAGE),(7,old)])
        self.assertEqual(self.conn.execute('SELECT value FROM delivery_counter').fetchone()[0],delivery)
        self.assertFalse(self.service.watch_state('bob@local')['changed'])
        self.assertEqual(self.conn.execute("SELECT client_token FROM emails WHERE id='eml_remote'").fetchone()[0],'cloud-import:v1:eml_remote')

    def test_differing_pull_rolls_back_matching_predecessor(self):
        self.pull([(1,MESSAGE)])
        replay(self.conn)
        changed=copy.deepcopy(MESSAGE); changed['body_markdown']='different'
        self.pull([(1,changed)])
        self.assertEqual(state(self.conn)['last_pulled_seq'],0)
        self.assertEqual(state(self.conn)['last_error'],'payload_conflict')

    def test_generation_reset_replay_then_restore_imports(self):
        self.pull([(7,MESSAGE)])
        self.service.mark_thread_read('bob@local','thr_remote')
        calls=[]
        def pull(cursor,generation,limit=200):
            calls.append((cursor,generation))
            if generation=='gen_a':
                raise CloudSyncError('reset',code='generation_mismatch',generation='gen_b')
            return page(cursor=cursor,generation='gen_b')
        self.http.pull.side_effect=pull
        self.engine.sync_once(self.conn)
        self.assertEqual(state(self.conn)['last_pulled_seq'],0)
        self.assertEqual(self.status('eml_remote')['state'],'pending')
        self.engine.sync_once(self.conn)
        self.assertIn((0,'gen_b'),calls)
        self.assertEqual(self.status('eml_remote')['ack_generation'],'gen_b')
        self.assertFalse(self.service.watch_state('bob@local')['changed'])
        self.assertEqual(self.http.push.call_args.args[0],[MESSAGE])

    def test_changed_generation_200_restarts_at_zero(self):
        self.conn.execute('UPDATE cloud_state SET last_pulled_seq=99')
        self.http.pull.side_effect=[page(generation='new',cursor=99),page([(1,MESSAGE)],generation='new')]
        self.engine.sync_once(self.conn)
        self.assertEqual(self.http.pull.call_args_list[1].args,(0,))
        self.assertEqual(state(self.conn)['last_pulled_seq'],1)

    def test_request_size_and_count_repacking(self):
        for n in range(18): self.send(str(n),'x'*60000)
        regular=self.http.push.side_effect
        def push(messages,generation):
            if len(messages)>3:
                raise CloudSyncError('size',code='request_too_large')
            return regular(messages,generation)
        self.http.push.side_effect=push
        self.engine.sync_once(self.conn)
        self.assertEqual(sync_status(self.conn)['counts']['acknowledged'],18)
        for call in self.http.push.call_args_list:
            request=dict(protocol_version=1,action='push',generation='gen_a',messages=call.args[0])
            self.assertLessEqual(len(canonical(request).encode()),1048576)
            self.assertLessEqual(len(call.args[0]),100)

    def test_unbound_or_wrong_endpoint_never_uploads(self):
        self.send()
        self.http.url='https://different.com'
        with self.assertRaises(ValueError): self.engine.sync_once(self.conn)
        self.http.push.assert_not_called()
        other=get_connection(self.path/'other'); self.addCleanup(other.close)
        with self.assertRaises(ValueError): self.engine.sync_once(other)
        with self.assertRaises(sqlite3.IntegrityError): self.conn.execute("UPDATE cloud_state SET user_id='different'")

    def test_login_mismatch_changes_nothing_and_rotation(self):
        save_config(dict(endpoint='https://example.com',device_id='dev_x',device_token='old',enabled=False))
        before=self.config.read_bytes(); old=state(self.conn)
        issued=dict(protocol_version=1,user_id='other',device_id='dev_x',device_token='secret',expires_at=TIME)
        with mock.patch.object(CloudClient,'issue_device',return_value=issued),mock.patch.object(CloudClient,'pull',return_value=page(user='other')):
            with self.assertRaises(ValueError): login(self.conn,'https://EXAMPLE.com:443/','website-secret')
        self.assertEqual(state(self.conn),old); self.assertEqual(self.config.read_bytes(),before)
        issued['user_id']='usr_a'
        with mock.patch.object(CloudClient,'issue_device',return_value=issued),mock.patch.object(CloudClient,'pull',return_value=page()):
            login(self.conn,'https://EXAMPLE.com:443/','website-secret')
        self.assertEqual(json.loads(self.config.read_text())['device_token'],'secret')
        self.assertNotIn('website-secret',self.config.read_text())
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(self.config.parent.stat().st_mode),0o700)

    def test_migrations_idempotent_preserve_delivery(self):
        mid=self.send(); delivery=self.conn.execute('SELECT delivery_id FROM emails').fetchone()[0]
        init_db(self.conn); init_db(self.conn)
        self.assertEqual(self.conn.execute('SELECT delivery_id FROM emails').fetchone()[0],delivery)
        self.assertEqual(state(self.conn)['user_id'],'usr_a')

    def test_reserved_token_and_mapping_collision(self):
        with self.assertRaises(Exception): self.send('cloud-import:v1:eml_test')
        # Clones of one repository may share a slug (spec); a second identity
        # mapping to an existing slug is legal, changing an identity's slug is not.
        map_project(self.conn,'https://other.com/repo','local')
        with self.assertRaises(ValueError): map_project(self.conn,'https://example.com/owner/local.git','changed')
        map_project(self.conn,'git@example.com:owner/local.git','local')

    def test_reserve_warning_success_and_failure_json(self):
        from agent_inbox.cli import build_parser,cmd_reserve
        save_config({'enabled':True})
        args=build_parser().parse_args(['reserve','file','--json'])
        client=mock.Mock(); client.acquire_reservations.return_value={'reservations':[]}
        with mock.patch('agent_inbox.cli.derive_identity',return_value=('bob','local','bob@local')),mock.patch('agent_inbox.cli.derive_project',return_value='local'):
            for failure in (False,True):
                if failure: client.acquire_reservations.side_effect=ValueError('bad')
                out=io.StringIO()
                with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()): cmd_reserve(args,client)
                self.assertIn(LOCAL_WARNING,json.loads(out.getvalue())['warnings'])

    def test_ack_snapshot_change_is_not_acknowledged(self):
        mid=self.send()
        regular=self.http.push.side_effect
        def push(messages,generation):
            self.conn.execute("UPDATE cloud_envelopes SET envelope_json='{}' WHERE message_id=?",(mid,))
            return regular(messages,generation)
        self.http.push.side_effect=push
        self.engine.sync_once(self.conn)
        self.assertNotEqual(self.status(mid)['state'],'acknowledged')
        self.assertIsNone(self.conn.execute('SELECT cloud_synced_at FROM emails').fetchone()[0])

    def test_matching_pull_acknowledges_rejected_mail(self):
        mid=self.send()
        self.http.push.side_effect=CloudSyncError('conflict',code='payload_conflict',ids=[mid])
        self.engine.sync_once(self.conn)
        frozen=strict_loads(self.status(mid)['envelope_json'])
        self.assertEqual(self.status(mid)['state'],'permanently_rejected')
        self.pull([(9,frozen)])
        self.assertEqual(self.status(mid)['state'],'acknowledged')
        self.assertEqual(self.status(mid)['ack_seq'],9)

    def test_thread_conflict_and_same_subject_identity(self):
        self.pull([(1,MESSAGE)])
        bad=child(MESSAGE)
        bad['thread']['subject']='different canonical identity'
        self.pull([(2,bad)])
        self.assertEqual(state(self.conn)['last_pulled_seq'],1)
        self.assertEqual(state(self.conn)['last_error'],'thread_conflict')
        other=copy.deepcopy(MESSAGE)
        other['message_id']='eml_other'; other['thread']['root_message_id']='eml_other'
        other['thread']['thread_id']='thr_other'
        self.clear_backoff(); self.pull([(2,other)])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM threads').fetchone()[0],2)

    def test_request_errors_retry_no_quarantine_and_backoff(self):
        mid=self.send()
        for code in ['invalid_request','temporarily_unavailable','rate_limited','global_capacity','transport_error']:
            self.clear_backoff()
            self.http.push.side_effect=CloudSyncError('retry',code=code,retry_after=40)
            before=time.time()
            self.engine.sync_once(self.conn)
            self.assertEqual(self.status(mid)['state'],'retryable')
            self.assertGreaterEqual(self.status(mid)['retry_at'],before+40)
            self.assertLessEqual(self.status(mid)['retry_at'],time.time()+900)

    def test_unknown_error_ids_cannot_quarantine(self):
        mid=self.send()
        self.http.push.side_effect=CloudSyncError('bad ids',code='invalid_message',ids=['eml_unsubmitted'])
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(mid)['state'],'retryable')

    def test_missing_ancestor_recovers_ancestors_without_hot_child_retry(self):
        root=self.send(); self.engine.sync_once(self.conn)
        reply=self.service.reply_email(root,'alice@remote','reply','reply')['email_id']
        regular=self.http.push.side_effect
        first=True
        def push(messages,generation):
            nonlocal first
            if first:
                first=False
                raise CloudSyncError('missing',code='missing_ancestor',ids=[reply])
            return regular(messages,generation)
        self.http.push.side_effect=push
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(root)['state'],'pending')
        self.assertEqual(self.status(reply)['reason'],'blocked_by_ancestor')
        self.engine.sync_once(self.conn)
        self.assertEqual([e['message_id'] for e in self.http.push.call_args.args[0]],[root])
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(reply)['state'],'acknowledged')

    def test_one_hundred_message_cap(self):
        for n in range(101): self.send(str(n))
        self.engine.sync_once(self.conn)
        self.assertEqual([len(call.args[0]) for call in self.http.push.call_args_list],[100,1])
        self.assertEqual(sync_status(self.conn)['counts']['acknowledged'],101)

    def test_drain_pull_pages(self):
        second=child(MESSAGE)
        self.http.pull.side_effect=[page([(1,MESSAGE)],more=True),page([(3,second)])]
        self.engine.sync_once(self.conn)
        self.assertEqual(state(self.conn)['last_pulled_seq'],3)
        self.assertEqual(self.http.pull.call_args_list[1].args,(1,))

    def test_legacy_time_conversion_and_local_projection(self):
        mid=self.send()
        self.conn.execute("UPDATE emails SET sent_at='2020-01-01T01:00:00+01:00'")
        self.conn.execute("UPDATE threads SET created_at='2020-01-01T01:00:00+01:00',last_email_at='2020-01-01T01:00:00+01:00'")
        self.conn.execute("UPDATE thread_inboxes SET joined_at='2020-01-01T01:00:00+01:00'")
        self.engine.sync_once(self.conn)
        self.assertEqual(strict_loads(self.status(mid)['envelope_json'])['sent_at'],TIME)
        with mock.patch('agent_inbox.service.utc_now_iso',return_value='2019-01-01T00:00:00.000Z'):
            self.service.reply_email(mid,'alice@remote','old author time','reply')
        self.assertEqual(self.conn.execute('SELECT last_email_at FROM threads').fetchone()[0],TIME)
        self.assertEqual(self.conn.execute('SELECT MIN(joined_at) FROM thread_inboxes').fetchone()[0],'2019-01-01T00:00:00.000Z')

    def test_unmapped_legacy_mail_waits_for_explicit_mapping(self):
        mid=self.service.send_email('bob@unmapped',['alice@remote'],[],'Legacy','body','legacy')['email_id']
        self.engine.sync_once(self.conn)
        self.http.push.assert_not_called()
        self.assertEqual(self.status(mid)['reason'],'unresolved_project_mapping')
        map_project(self.conn,'https://example.com/team/unmapped','unmapped')
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(mid)['state'],'acknowledged')

    def test_consistent_backup_retains_delivery_and_hook_stamps(self):
        self.pull([(1,MESSAGE)])
        threads=self.service.list_threads('bob@local',unread_only=True)
        self.assertIsNotNone(hooks.build_notice('bob@local',threads,now=1000))
        backup=sqlite3.connect(self.path/'backup')
        self.conn.backup(backup); backup.close()
        restored=get_connection(self.path/'backup'); self.addCleanup(restored.close)
        replay(restored)
        self.http.pull.return_value=page([(1,MESSAGE)])
        self.engine.sync_once(restored)
        self.assertEqual(restored.execute('SELECT value FROM delivery_counter').fetchone()[0],1)
        with mock.patch('agent_inbox.hooks.get_connection',side_effect=lambda:get_connection(self.path/'backup')):
            self.assertIsNone(hooks.build_notice('bob@local',InboxService(restored).list_threads('bob@local',unread_only=True),now=1001))

    def test_cli_login_has_no_argv_secret_and_status_is_local(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            main(['cloud','login','--token','must-not-accept'])
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            code=main(['cloud','status','--json','--db',str(self.path/'db')])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(out.getvalue())['user_id'],'usr_a')

    def test_full_protocol_address_alphabet_and_length_survive_import(self):
        e=copy.deepcopy(MESSAGE)
        project='remote.project_' + 'x'*100
        e['thread']['home_project']=project
        e['sender']='a.b_c@'+project
        e['recipients']['to']=['b._@local.project']
        self.pull([(1,e)])
        self.assertEqual(state(self.conn)['last_pulled_seq'],1)
        thread=self.service.get_thread('b._@local.project','thr_remote')
        self.assertEqual(thread['emails'][0]['from'],'a.b_c@'+project)
        reply=self.service.reply_email('eml_remote','b._@local.project','reply','reply')
        self.assertEqual(reply['to'],['a.b_c@'+project])

    def test_reply_to_imported_thread_exports_without_remote_repo_mapping(self):
        self.pull([(1,MESSAGE)])
        reply=self.service.reply_email('eml_remote','bob@local','reply','reply')['email_id']
        self.http.pull.return_value=page(cursor=1)
        self.engine.sync_once(self.conn)
        self.assertEqual(self.status(reply)['state'],'acknowledged')
        self.assertEqual(self.http.push.call_args.args[0][0]['thread'],MESSAGE['thread'])

    def test_identity_mapping_shared_across_clone_transport(self):
        from agent_inbox.identity import derive_project
        save_config({'enabled':True})
        with mock.patch('agent_inbox.identity.subprocess.run',return_value=mock.Mock(stdout='git@example.com:owner/local.git')):
            with mock.patch.dict(os.environ,{'AGENT_INBOX_PROJECT':'local'}):
                self.assertEqual(derive_project(),'local')
            with mock.patch.dict(os.environ,{'AGENT_INBOX_PROJECT':''}):
                self.assertEqual(derive_project(),'local')

    def test_initial_login_binds_before_upload_and_import(self):
        other=get_connection(self.path/'unbound'); self.addCleanup(other.close)
        issued=dict(protocol_version=1,user_id='usr_new',device_id='dev_test',device_token='mail-secret',expires_at=TIME)
        save_config(dict(device_id='dev_test',enabled=False))
        with mock.patch.object(CloudClient,'issue_device',return_value=issued),mock.patch.object(CloudClient,'pull',return_value=page(user='usr_new')),mock.patch.object(CloudClient,'push') as push:
            login(other,'https://EXAMPLE.com:443/','website-secret')
            push.assert_not_called()
        self.assertEqual((state(other)['endpoint'],state(other)['user_id']),('https://example.com','usr_new'))
        self.assertEqual(state(other)['last_pulled_seq'],0)

    def test_worker_lock_prevents_second_worker_network(self):
        import fcntl
        lock_path=Path(str((self.path/'db').resolve())+'.cloud.lock')
        with lock_path.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with mock.patch('agent_inbox.cloudsync.CloudClient') as client:
                SyncWorker(self.path/'db').run()
                client.assert_not_called()

    def test_worker_signed_out_no_network(self):
        worker=SyncWorker(self.path/'db')
        with mock.patch('agent_inbox.cloudsync.CloudClient') as client:
            worker.run()
            client.assert_not_called()


class ProtocolTest(unittest.TestCase):
    def test_canonical_unicode_and_duplicate_keys(self):
        e=copy.deepcopy(MESSAGE); e['body_markdown']='é\n😀\t"'
        payload=validate_envelope(e)
        self.assertIn('é',payload); self.assertIn('😀',payload)
        self.assertNotIn(': ',payload)
        self.assertEqual(strict_loads(payload),e)
        with self.assertRaises(ValueError): strict_loads('{"a":1,"a":2}')

    def test_strict_envelope_invalid_fields_types_and_bounds(self):
        for field,value in [('extra',None),('envelope_version',True),('body_markdown','x'*65537),('body_markdown','\ud800'),('body_markdown','\0'),('subject',' '),('sent_at','2020-02-30T00:00:00.000Z'),('sender','Alice@remote'),('origin_device',''),('references',['eml_bad'])]:
            with self.subTest(field=field,value=str(value)[:40]):
                e=copy.deepcopy(MESSAGE); e[field]=value
                with self.assertRaises((ValueError,UnicodeError)): validate_envelope(e)
        e=copy.deepcopy(MESSAGE); e['recipients']['cc']=['bob@local']
        with self.assertRaises(ValueError): validate_envelope(e)

    def test_pull_exact_cursor_and_identity(self):
        for response in [page([(2,MESSAGE)],cursor=0)|{'last_seq':3}, page(cursor=1,more=True),page([(1,MESSAGE),(1,MESSAGE)]),page(user='other')]:
            with self.assertRaises(ValueError): validate_pull(response,0,'usr_a','gen_a')

    def test_endpoint_origins(self):
        self.assertEqual(endpoint_origin('https://EXAMPLE.com:443/'),'https://example.com')
        for url in ['http://example.com','https://user@example.com','https://example.com/path','https://example.com?','https://example.com/#x']:
            with self.assertRaises(CloudSyncError): endpoint_origin(url)

    def test_http_device_wire_timeout_and_redirect_refusal(self):
        response=mock.Mock(status=302)
        response.read1.side_effect=[b'{}',b'']
        response.getheader.return_value='0'
        connection=mock.Mock(); connection.getresponse.return_value=response
        with mock.patch('http.client.HTTPSConnection',return_value=connection) as constructor:
            with self.assertRaises(CloudSyncError): CloudClient('https://example.com','secret').pull(0)
        constructor.assert_called_once_with('example.com',None,timeout=5)
        self.assertEqual(connection.request.call_count,1)
        data=json.loads(connection.request.call_args.kwargs['body'])
        self.assertEqual(data,dict(protocol_version=1,action='pull',generation=None,after_seq=0,limit=200))


if __name__=='__main__': unittest.main()
