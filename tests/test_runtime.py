import json
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from runtime import Resident,Tasks,resource_snapshot,tail,event_time
from decision import decision


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        (self.root/'board').mkdir(); (self.root/'telemetry').mkdir()
        self.env=dict(os.environ,AGENT_ID='01-a',AGENT_NAME='a',AGENT_ROLE='resident',VILLAGE_ROOT=str(self.root),
                      VILLAGE_MAX_OUTPUT_BYTES='4096',VILLAGE_COMMAND_TIMEOUT_SECONDS='2')
        self.agent=Resident(self.env)
    def tearDown(self): self.tmp.cleanup()
    def execute(self,name,**args): self.agent.execute({'tool_call':{'name':name,'arguments':args}})

    def test_result_survives_restart_and_pipeline_fails(self):
        self.execute('execute_bash',command='printf observable-result; false | true')
        again=Resident(self.env)
        self.assertFalse(again.state['last_result']['ok'])
        self.assertIn('observable-result',again.state['last_result']['result'])

    def test_no_permanent_failure_deadlock(self):
        for _ in range(3): self.execute('execute_bash',command='false')
        self.assertIn('Repeated action blocked',self.agent.state['last_result']['result'])
        self.execute('idle')
        self.execute('execute_bash',command='false')
        self.assertIn('Repeated action blocked',self.agent.state['last_result']['result'])
        self.execute('execute_bash',command='printf recovered')
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_bounded_output_and_timeout(self):
        self.execute('execute_bash',command="head -c 50000 /dev/zero; sleep 10")
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertLessEqual((self.agent.home/'last-command.log').stat().st_size,4200)

    def test_tasks_claim_and_evidence(self):
        tasks=Tasks(self.root/'board')
        item=tasks.operate('a',dict(action='create',title='test',success_criterion='reproduce output'))
        self.assertEqual(tasks.path.stat().st_mode&0o777,0o660)
        tasks.operate('a',dict(action='claim',task_id=item['id']))
        with self.assertRaises(ValueError): tasks.operate('b',dict(action='claim',task_id=item['id']))
        with self.assertRaises(ValueError): tasks.operate('a',dict(action='complete',task_id=item['id']))
        result=tasks.operate('a',dict(action='complete',task_id=item['id'],evidence='artifact:test'))
        self.assertFalse(result['completion_verified'])

    def test_invalid_output_not_broadcast_as_peer_instruction(self):
        self.agent.execute(decision({'message':{'content':'{"name":'}}))
        self.assertEqual(tail(self.root/'board/events.jsonl')[0]['event'],'invalid_decision')
        self.assertEqual(self.agent.state['invalid_streak'],1)

    def test_resource_snapshot_is_locale_independent(self):
        with patch.dict(os.environ,{'LANG':'de_DE.UTF-8'}):
            value=resource_snapshot(self.root)
        self.assertGreater(value['ram_available_mib'],0)
        self.assertLessEqual(value['ram_available_mib'],value['ram_total_mib'])

    def test_partial_log_does_not_erase_valid_events(self):
        path=self.root/'events'; path.write_text('{broken\n{"event":"ok"}\n')
        self.assertEqual(tail(path),[{'event':'ok'}])

    def test_timestamp_order_handles_legacy_timezones(self):
        self.assertLess(event_time({'timestamp':'2026-09-24T17:00:00+02:00'}),
                        event_time({'timestamp':'2026-09-24T15:01:00+00:00'}))

    def test_secret_redaction(self):
        self.agent.env['MEMORY_AGENT_TOKEN']='very-private'
        self.execute('execute_bash',command='printf harmless')
        self.agent.event('test','very-private')
        self.assertNotIn('very-private',(self.root/'board/events.jsonl').read_text())

    def test_task_envelope_is_supported(self):
        obj={'name':'task_operation','arguments':{'action':'claim','task_id':'123'}}
        parsed=decision({'message':{'content':json.dumps(obj)}})
        self.assertNotIn('fallback_reason',parsed)

    def test_organic_message_not_reissued_every_turn(self):
        (self.root/'board/organic-inbox.jsonl').write_text(json.dumps({'timestamp':'2026-09-24T11:00:00Z','message':'A dated request'})+'\n')
        with patch.object(self.agent,'memory',return_value={'items':[]}):
            first=json.loads(self.agent.snapshot())
            self.assertEqual(len(first['recent_organic_messages_untrusted']),1)
            self.agent.state['seen_organic_epoch']=self.agent.pending_organic_cursor
            second=json.loads(self.agent.snapshot())
            self.assertEqual(second['recent_organic_messages_untrusted'],[])

    def test_inference_preserves_settings_without_json_response_constraint(self):
        self.agent.env.update(OLLAMA_MODEL='test-model',OLLAMA_URL='http://localhost:1234',
            OLLAMA_NUM_CTX='12345',OLLAMA_NUM_PREDICT='321',OLLAMA_KEEP_ALIVE='24h',
            OLLAMA_THINK_LEVEL='medium',AGENT_IDENTITY_PROMPT='/test/identity')
        self.agent.pending_cursor=1; self.agent.pending_organic_cursor=2
        answer={'done_reason':'stop','message':{'content':'{"name":"idle","arguments":{}}'}}
        with patch.object(self.agent,'snapshot',return_value='{}'), patch('runtime.Path.read_text',return_value='Test identity'), patch('runtime.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(answer).encode())) as request:
            self.agent.cycle()
        payload=json.loads(request.call_args.args[0].data)
        self.assertNotIn('format',payload)
        self.assertEqual(payload['options']['num_ctx'],12345)
        self.assertEqual(payload['options']['num_predict'],321)
        self.assertEqual(payload['keep_alive'],'24h')
        self.assertEqual(payload['think'],'medium')
        self.assertEqual(self.agent.state['last_result']['action'],'idle')
        self.assertEqual(self.agent.state['seen_organic_epoch'],2)

    def test_inference_openai_mode_in_runtime(self):
        self.agent.env.update(API_TYPE='openai',OLLAMA_MODEL='gpt-4o-mini',OLLAMA_URL='https://api.openai.com/v1',
            OLLAMA_NUM_PREDICT='512',API_TOKEN='test-token-synthetic',AGENT_IDENTITY_PROMPT='/test/identity')
        self.agent.pending_cursor = 1; self.agent.pending_organic_cursor = 2
        openai_resp = {
            'choices': [{'message': {'role': 'assistant', 'content': 'Ich ruhe mich aus.\n```village-action\n{"name":"idle"}\n```'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 120, 'completion_tokens': 40}
        }
        with patch.object(self.agent,'snapshot',return_value='{}'), patch('runtime.Path.read_text',return_value='Test identity'), patch('runtime.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(openai_resp).encode())) as request:
            self.agent.cycle()
        req_obj = request.call_args.args[0]
        self.assertIn('/chat/completions', req_obj.full_url)
        self.assertEqual(req_obj.headers.get('Authorization'), 'Bearer test-token-synthetic')
        payload = json.loads(req_obj.data)
        self.assertEqual(payload['model'], 'gpt-4o-mini')
        self.assertEqual(payload['max_tokens'], 512)
        self.assertNotIn('options', payload)
        self.assertEqual(self.agent.state['last_result']['action'], 'idle')


if __name__=='__main__': unittest.main()
