"""Agent-facing context and work commands, independent of messaging protocol."""
import argparse
import json
import sys
import uuid


def add_parser(subparsers):
    parser=subparsers.add_parser('ae',help='Restore context, manage work and receive coordinated events')
    parser.add_argument('--actor',help='Mailbox address; defaults to current agent')
    parser.add_argument('--session',help='Runtime session; defaults to current session')
    parser.add_argument('--request-id',help='Reuse for retries of an identical mutation')
    commands=parser.add_subparsers(dest='ae_command',required=True)
    commands.add_parser('setup',help='Compatibility check: AE needs no broker or extra setup')
    commands.add_parser('status',help='Show AE transport health')
    for name in ('brief','watch'):
        bp=commands.add_parser(name,help='Bounded context and changes' if name=='brief' else 'Batch relevant changes; urgent events return immediately')
        bp.add_argument('--after',type=int,required=name=='watch')
        bp.add_argument('--source',required=name=='watch')
        bp.add_argument('--limit',type=int,default=20)
        bp.add_argument('--policy',choices=['all','to-me','my-tasks','my-files','my-work'],default='all')
        if name=='watch':
            bp.add_argument('--timeout',type=float,default=60)
            bp.add_argument('--coalesce',type=float,default=30)
    short=subparsers.add_parser('brief',help='Restore AE context and changes in one read')
    short.add_argument('--actor');short.add_argument('--session');short.add_argument('--after',type=int);short.add_argument('--source')
    short.add_argument('--limit',type=int,default=20);short.add_argument('--policy',choices=['all','to-me','my-tasks','my-files','my-work'],default='all')
    short.set_defaults(ae_command='brief',request_id=None)
    context=commands.add_parser('context' ,help='Restore the bounded working context')
    context.add_argument('--limit',type=int,default=20)
    events=commands.add_parser('events',help='Receive relevant events after a source-scoped cursor')
    events.add_argument('--after',type=int,default=0);events.add_argument('--source')
    events.add_argument('--limit',type=int,default=100);events.add_argument('--wait',type=float,default=0)
    task=commands.add_parser('task',help='Create, inspect, claim and hand off durable work')
    operations=task.add_subparsers(dest='task_action',required=True)
    create=operations.add_parser('create')
    create.add_argument('--title',required=True);create.add_argument('--description',default='')
    create.add_argument('--target');create.add_argument('--priority',type=int,choices=range(4),default=1)
    create.add_argument('--depends-on',action='append',default=[]);create.add_argument('--path',action='append',default=[])
    create.add_argument('--thread')
    listing=operations.add_parser('list');listing.add_argument('--state',choices=['queued','active','blocked','completed']);listing.add_argument('--after',default='');listing.add_argument('--limit',type=int,default=50)
    read=operations.add_parser('get');read.add_argument('id')
    history=operations.add_parser('history');history.add_argument('id');history.add_argument('--after',type=int,default=0);history.add_argument('--limit',type=int,default=100)
    for action in ('claim','block','resume','complete','handoff'):
        p=operations.add_parser(action);p.add_argument('id');p.add_argument('--version',type=int,required=True)
        if action in ('block','resume','handoff'):p.add_argument('--note',required=True)
        if action=='complete':p.add_argument('--result',required=True)
        if action=='handoff':
            p.add_argument('--target')
            for field in ('next-action','workspace','ref','acceptance','evidence'):
                p.add_argument('--'+field,required=True)
    for action in ('subscribe','unsubscribe'):
        p=commands.add_parser(action);p.add_argument('kind',choices=['task','thread']);p.add_argument('ref')
    decisions=commands.add_parser('decision').add_subparsers(dest='decision_action',required=True)
    decision=decisions.add_parser('record');decision.add_argument('--title',required=True)
    decision.add_argument('--body',required=True);decision.add_argument('--source-ref',required=True)
    decision_get=decisions.add_parser('get');decision_get.add_argument('id')
    ack=commands.add_parser('ack');ack.add_argument('sequence',type=int);ack.add_argument('--source',required=True)


def run(args):
    from agent_inbox.client import InboxClient
    from agent_inbox.identity import derive_identity,derive_session
    from urllib.parse import quote
    try:
        command=args.ae_command
        if command=='setup':
            print(json.dumps({'broker_required':False,'message':'No extra setup required. Start agent-inbox serve.'}));return 0
        actor=args.actor or derive_identity()[2];session=args.session or derive_session()
        client=InboxClient(session_id=session)
        if command in ('brief','watch'):
            query={'actor':actor,'session':session,'after':args.after,'source':args.source,'limit':args.limit,'policy':args.policy}
            if command=='watch':query.update(timeout=args.timeout,coalesce=args.coalesce)
            result=client._request('GET','/v1/ae/'+command,query=query,timeout=70 if command=='watch' else 10)
        elif command=='context':
            result=client._request('GET','/v1/ae/context',query={'actor':actor,'session':session,'limit':args.limit})
        elif command=='status':result=client._request('GET','/v1/ae/status')
        elif command=='events':
            result=client._request('GET','/v1/ae/events',query={'actor':actor,'after':args.after,'source':args.source,'limit':args.limit,'wait':args.wait},timeout=min(max(args.wait,0),60)+10)
        elif command=='decision' and args.decision_action=='get':
            result=client._request('GET','/v1/ae/decisions/'+quote(args.id,safe=''),query={'actor':actor})
        elif command=='task' and args.task_action=='list':
            result=client._request('GET','/v1/ae/tasks',query={'actor':actor,'state':args.state,'after':args.after,'limit':args.limit})
        elif command=='task' and args.task_action=='history':
            result=client._request('GET','/v1/ae/task-history/'+quote(args.id,safe=''),query={'actor':actor,'after':args.after,'limit':args.limit})
        elif command=='task' and args.task_action=='get':
            result=client._request('GET','/v1/ae/tasks/'+quote(args.id,safe=''),query={'actor':actor})
        else:
            if command=='task':
                operation='task.'+args.task_action
                if args.task_action=='create':
                    payload={'title':args.title,'description':args.description,'target':args.target,'priority':args.priority,'dependencies':args.depends_on,'paths':args.path,'thread_id':args.thread}
                else:
                    payload={'id':args.id,'version':args.version}
                    if args.task_action=='handoff':
                        payload['handoff']={k:getattr(args,k) for k in ('next_action','workspace','ref','acceptance','evidence')}
                    for k in ('note','result','target'):
                        if hasattr(args,k):payload[k]=getattr(args,k)
            elif command in ('subscribe','unsubscribe'):operation=command;payload={'kind':args.kind,'ref':args.ref}
            elif command=='decision':operation='decision.record';payload={'title':args.title,'body':args.body,'source_ref':args.source_ref}
            else:operation='ack';payload={'source':args.source,'sequence':args.sequence}
            result=client._request('POST','/v1/ae/command',body={'actor':actor,'session':session,'request_id':args.request_id or str(uuid.uuid4()),'operation':operation,'payload':payload})
        print(json.dumps(result));return 3 if command=='watch' and not result['changed'] and not result['has_more'] else 0
    except Exception as exc:
        print(f'AE error: {exc}',file=sys.stderr);return 1
