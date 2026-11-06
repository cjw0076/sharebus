"""Official SDK + owned local scripted pipes; never a native CLI or model."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any
import unittest

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
SPEC = importlib.util.spec_from_file_location("sdk_contract_native_adapter", SOURCE / "adapters.py")
assert SPEC and SPEC.loader
native = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = native
SPEC.loader.exec_module(native)
NativeAdapter: Any = native.NativeAdapter
from antigravity_acp import AntigravityACP

MODEL, SESSION = "gemini-4-argon-high", "exact-native-owned-session"
ALLOWED = frozenset({"gemini-4-argon-low", "gemini-4-argon-medium", MODEL,
                     "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"})
SCRIPT = r'''
import copy,json,os,sys
behavior, stats_path = sys.argv[1:]
stats={'methods':[], 'prompt_count':0, 'responses':[]}
configs=[{'id':'model','name':'Model','category':'model','type':'select','currentValue':'gemini-4-argon',
          'options':[{'value':x,'name':x} for x in ['gemini-4-argon','gemini-3.8-flash']]},
         {'id':'thought_level','name':'Thinking','category':'thought_level','type':'select','currentValue':'medium',
          'options':[{'value':x,'name':x} for x in ['low','medium','high']]},
         {'id':'mode','name':'Mode','category':'mode','type':'select','currentValue':'default',
          'options':[{'value':'default','name':'Default'},{'value':'accept-edits','name':'Auto'}]}]
prompt_id=None
session='exact-native-owned-session'
def emit(value): print(json.dumps(value),flush=True)
def reply(i,result): emit({'jsonrpc':'2.0','id':i,'result':result})
def save():
    with open(stats_path,'w') as f: json.dump(stats,f)
def update(kind,text):
    emit({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,
      'update':{'sessionUpdate':kind,'content':{'type':'text','text':text}}}})
def complete(stop='end_turn'):
    update('agent_message_chunk','public answer')
    reply(prompt_id,{'stopReason':stop})
def permission():
    emit({'jsonrpc':'2.0','id':'actual-permission-42','method':'session/request_permission','params':{
      'sessionId':session,'toolCall':{'toolCallId':'native-tool-7','title':'Run Python version','kind':'execute',
      'locations':[{'path':os.path.join(os.getcwd(),'source.py')}],
      'rawInput':{'CommandLine':'python -V','WorkingDirectory':os.getcwd(),'body':'PRIVATE_TOOL_INPUT'},
      'rawOutput':'PRIVATE_TOOL_OUTPUT','content':[]},'options':[
      {'optionId':'native-always','name':'Always','kind':'allow_always'},
      {'optionId':'native-once-real','name':'Once','kind':'allow_once'},
      {'optionId':'native-reject-real','name':'Reject','kind':'reject_once'}]}})
for line in sys.stdin:
    packet=json.loads(line); method=packet.get('method'); i=packet.get('id')
    if method: stats['methods'].append(method)
    save()
    if method=='initialize':
        reply(i,{'protocolVersion':1,'agentCapabilities':{'loadSession':behavior!='unsupported-resume'}})
    elif method in ['session/new','session/load']:
        if behavior=='wrong-session': session='different-session'
        state={'models':{'currentModelId':'gemini-4-argon-medium',
           'availableModels':[{'modelId':'gemini-4-argon-'+x,'name':x} for x in ['low','medium','high']]},
           'configOptions':configs}
        if method=='session/new' or behavior=='wrong-session': state['sessionId']=session
        update('user_message_chunk','PRIVATE_HISTORY')
        reply(i,state)
    elif method=='session/set_config_option':
        q=packet['params']
        if behavior in ['delayed-duplicate','startup-round-trip'] and q['configId']=='thought_level':
            if behavior=='startup-round-trip':
                changed=copy.deepcopy(configs); changed[0]['currentValue']='gemini-3.8-flash'
                emit({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,
                    'update':{'sessionUpdate':'config_option_update','configOptions':changed}}})
            emit({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,
                'update':{'sessionUpdate':'config_option_update','configOptions':configs}}})
        for config in configs:
            if config['id']==q['configId']: config['currentValue']=q['value']
        if behavior=='unconfirmed-model' and q['configId']=='thought_level': configs[1]['currentValue']='medium'
        if behavior=='startup-race' and q['configId']=='thought_level':
            changed=copy.deepcopy(configs); changed[0]['currentValue']='gemini-3.8-flash'
            emit({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,
                'update':{'sessionUpdate':'config_option_update','configOptions':changed}}})
        reply('unrelated-state-response' if behavior=='wrong-response-id' and q['configId']=='thought_level' else i,
              {'configOptions':configs[:1] if behavior=='missing-model-ack' and q['configId']=='thought_level' else configs})
        if behavior=='post-response-change' and q['configId']=='thought_level':
            changed=copy.deepcopy(configs); changed[0]['currentValue']='gemini-3.8-flash'
            emit({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,
                'update':{'sessionUpdate':'config_option_update','configOptions':changed}}})
    elif method=='session/prompt':
        prompt_id=i; stats['prompt_count']+=1; save()
        update('agent_thought_chunk','PRIVATE_THOUGHT')
        update('user_message_chunk','PRIVATE_USER_ECHO')
        if behavior=='permission': permission()
        elif behavior=='timeout': pass
        elif behavior=='unsupported':
            emit({'jsonrpc':'2.0','id':'actual-fs-request','method':'fs/read_text_file',
                  'params':{'sessionId':session,'path':'PRIVATE_PATH'}})
        elif behavior=='oversized':
            print('x'*8193,flush=True)
        elif behavior=='malformed-private':
            emit({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':session,
              'update':{'sessionUpdate':'agent_thought_chunk','content':{'type':'unsupported-private',
              'text':'PRIVATE_SCHEMA_SENTINEL'}}}})
            complete()
        elif behavior=='error':
            emit({'jsonrpc':'2.0','id':i,'error':{'code':-32603,'message':'PRIVATE_MODEL_ERROR'}})
        elif behavior=='late-permission':
            complete(); permission()
        else: complete()
    elif method=='session/cancel': complete('cancelled')
    elif i=='actual-permission-42':
        stats['responses'].append({'id':i,'result':packet.get('result')}); save()
        if packet.get('result',{}).get('outcome',{}).get('outcome')!='cancelled': complete()
'''


class ScriptedAdapter(NativeAdapter):
    def __init__(self, workspace, script, stats, behavior="normal", model=MODEL, wire_limit=1048576):
        self.provider, self.workspace, self.agent_id = "antigravity", workspace, "owned-agent"
        self.token_file, self.mcp_python = workspace / "owned.token", sys.executable
        self.startup_timeout = 5
        self.session_id = self._expected_session = self._turn_id = self._deadline = None
        self._reader_state, self._approval = {}, None
        self._approval_lock, self._turn_lock = threading.Lock(), threading.Lock()
        self._interrupted = threading.Event()
        self._needs_human = self._input_attempted = self._accepted = False
        self._input_rejected = self._delivery_unknown = False
        self.events, self.on_approval = [], None
        self.on_event = self.events.append
        self._acp = AntigravityACP(self, needs_human=native.NativeNeedsHuman, allowed_models=ALLOWED,
                                   max_wire_bytes=wire_limit)
        self.command = [sys.executable, "-I", "-B", "-u", str(script), behavior, str(stats)]
        self.model, self.stats = model, stats

    @property
    def pid(self): return self._acp.pid
    def close(self): self._acp.close()
    def start(self, resume_id=None):
        self._expected_session=resume_id
        return self._acp.start(self.command, dict(os.environ), resume_id, self.model)
    def _stream_turn(self, prompt, timeout): return self._acp.turn(prompt, timeout)
    def respond_approval(self, approval_id, result): return self._acp.respond_approval(approval_id, result)
    def _check_workspace(self): pass


@unittest.skipUnless(importlib.util.find_spec("acp"), "official ACP SDK runtime required")
class SDKPipeContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acp-sdk-contract-")
        self.workspace = Path(self.temp.name)
        self.script = self.workspace / "scripted_agent.py"
        self.script.write_text(SCRIPT, encoding="utf-8")
        self.adapters=[]

    def tearDown(self):
        for adapter in self.adapters:
            adapter.close()
            self.assertIsNone(adapter.pid)
            self.assertFalse(adapter._acp._thread and adapter._acp._thread.is_alive())
            self.assertNotIn(adapter._acp._log_filter,logging.getLogger().filters)
        self.temp.cleanup()

    def adapter(self, behavior="normal", **kwargs):
        adapter=ScriptedAdapter(self.workspace,self.script,self.workspace/(str(len(self.adapters))+".json"),behavior,**kwargs)
        self.adapters.append(adapter)
        return adapter

    def stats(self, adapter): return json.loads(adapter.stats.read_text())

    def test_two_turns_exact_resume_model_and_no_private_retention(self):
        adapter=self.adapter()
        self.assertEqual(adapter.start(SESSION),SESSION)
        for prompt in ["first","second"]:
            result=adapter.turn(prompt,timeout=3)
            self.assertTrue(result.success)
            self.assertEqual(result.text,"public answer")
            self.assertIsNone(result.native_turn_id)
        stats=self.stats(adapter)
        self.assertEqual(stats['prompt_count'],2)
        self.assertNotIn('session/new',stats['methods'])
        self.assertEqual(stats['methods'].count('session/set_config_option'),2)
        self.assertEqual(sum(x['type']=='input_attempt' for x in adapter.events),2)
        self.assertNotIn('PRIVATE_',json.dumps(adapter.events))
        self.assertEqual(adapter._acp._text,'')
        self.assertEqual(adapter._acp._model,MODEL)
        adapter.close()
        self.assertEqual(adapter._acp._process.returncode,0)

    def test_null_preserves_medium_without_config_override(self):
        adapter=self.adapter(model=None)
        adapter.start()
        self.assertEqual(adapter._acp._model,'gemini-4-argon-medium')
        self.assertNotIn('session/set_config_option',self.stats(adapter)['methods'])
        self.assertTrue(adapter.turn('preserve',timeout=3).success)
        adapter.close()
        self.assertEqual(adapter._acp._process.returncode,0)

    def test_setup_mismatch_missing_capability_and_startup_notification_race(self):
        for behavior in ['wrong-session','unsupported-resume','unconfirmed-model','startup-race','startup-round-trip']:
            with self.subTest(behavior=behavior):
                adapter=self.adapter(behavior)
                with self.assertRaises(RuntimeError): adapter.start(SESSION)
                self.assertFalse(adapter._input_attempted)
                self.assertEqual(self.stats(adapter)['prompt_count'],0)
                adapter.close()

    def test_delayed_duplicate_notification_does_not_discard_complete_config_response(self):
        adapter=self.adapter('delayed-duplicate')
        self.assertEqual(adapter.start(SESSION),SESSION)
        self.assertEqual(adapter._acp._model,MODEL)
        self.assertEqual({item['id']:item['currentValue'] for item in adapter._acp._configs},
                         {'model':'gemini-4-argon','thought_level':'high'})
        self.assertEqual(self.stats(adapter)['methods'].count('session/set_config_option'),2)
        self.assertEqual(self.stats(adapter)['prompt_count'],0)
        self.assertFalse(adapter._input_attempted)
        adapter.close()
        self.assertEqual(adapter._acp._process.returncode,0)

    def test_missing_model_ack_and_unrelated_response_id_never_send_input(self):
        for behavior,error in [('missing-model-ack',native.NativeNeedsHuman),('wrong-response-id',TimeoutError)]:
            with self.subTest(behavior=behavior):
                adapter=self.adapter(behavior)
                adapter.startup_timeout=2
                with self.assertRaises(error): adapter.start(SESSION)
                self.assertEqual(self.stats(adapter)['methods'].count('session/set_config_option'),2)
                self.assertEqual(self.stats(adapter)['prompt_count'],0)
                self.assertFalse(adapter._input_attempted)

    def test_notification_after_config_response_still_refuses_model_change(self):
        adapter=self.adapter('post-response-change')
        started=False
        try:
            adapter.start(SESSION)
            started=True
        except native.NativeNeedsHuman:
            pass  # The later notification can reach the final startup guard first.
        deadline=time.monotonic()+2
        while not adapter._acp._configs or adapter._acp._configs[0]['currentValue']!='gemini-3.8-flash':
            self.assertLess(time.monotonic(),deadline)
            time.sleep(0.01)
        if started:
            self.assertEqual(adapter._acp._error,'ACP model selection changed; input refused.')
        self.assertEqual(self.stats(adapter)['prompt_count'],0)
        self.assertFalse(adapter._input_attempted)

    def test_real_human_once_deny_cancel_and_native_title_kind_aliases(self):
        for decision,option in [('accept','native-once-real'),('decline','native-reject-real'),('cancel',None)]:
            with self.subTest(decision=decision):
                adapter=self.adapter('permission'); adapter.start()
                views=[]
                def human(view):
                    views.append(view)
                    self.assertEqual(view['request_id'],'actual-permission-42')
                    self.assertEqual(view['details']['tool_call']['title'],'Run Python version')
                    self.assertEqual(view['details']['tool_call']['kind'],'execute')
                    self.assertEqual(view['details']['command'],'python -V')
                    self.assertEqual(view['details']['cwd'],str(self.workspace))
                    self.assertNotIn('PRIVATE_',json.dumps(view))
                    self.assertNotIn('turn_id',view)
                    with self.assertRaises(ValueError):
                        adapter.respond_approval(view['approval_id'],{'decision':'accept','optionId':'native-always'})
                    adapter.respond_approval(view['approval_id'],{'decision':decision})
                    with self.assertRaises(ValueError): adapter.respond_approval(view['approval_id'],{'decision':decision})
                adapter.on_approval=human
                result=adapter.turn('one owned input',timeout=3)
                self.assertEqual(result.success,decision=='accept')
                self.assertFalse(result.delivery_unknown)
                self.assertEqual(len(views),1)
                outcome={'outcome':'selected','optionId':option} if option else {'outcome':'cancelled'}
                self.assertEqual(self.stats(adapter)['responses'],[{'id':'actual-permission-42','result':{'outcome':outcome}}])
                adapter.close()
                if decision=='accept':
                    self.assertEqual(adapter._acp._process.returncode,0)

    def test_timeout_error_unsupported_and_bounded_bytes_keep_unknown_no_replay(self):
        for behavior in ['timeout','error','unsupported','oversized']:
            with self.subTest(behavior=behavior):
                adapter=self.adapter(behavior,wire_limit=4096 if behavior=='oversized' else 1048576)
                adapter.start()
                result=adapter.turn('once',timeout=0.2 if behavior=='timeout' else 3)
                self.assertFalse(result.success)
                self.assertTrue(result.delivery_unknown)
                self.assertNotIn('PRIVATE_',str(result))
                self.assertTrue(adapter.turn('no replay').delivery_unknown)
                self.assertEqual(self.stats(adapter)['prompt_count'],1)
                adapter.close()

    def test_required_durable_receipt_failure_precedes_sdk_prompt(self):
        adapter=self.adapter(); adapter.start()
        def observer(event):
            if event['type']=='input_attempt': raise RuntimeError('PRIVATE_OBSERVER_ERROR')
        adapter.on_event=observer
        result=adapter.turn('must not send',timeout=3)
        self.assertFalse(result.input_attempted)
        self.assertFalse(result.delivery_unknown)
        self.assertEqual(result.reason_code,'native_observer_failed')
        self.assertEqual(self.stats(adapter)['prompt_count'],0)

    def test_late_human_response_after_timeout_never_sends_permission(self):
        adapter=self.adapter('permission'); adapter.start()
        views=[]; adapter.on_approval=views.append
        result=adapter.turn('once',timeout=0.2)
        self.assertTrue(result.delivery_unknown)
        self.assertEqual(len(views),1)
        with self.assertRaises(ValueError): adapter.respond_approval(views[0]['approval_id'],{'decision':'accept'})
        self.assertEqual(self.stats(adapter)['responses'],[])

    def test_sdk_malformed_private_logging_is_scoped_and_filter_is_removed(self):
        root=logging.getLogger()
        original_filters=list(root.filters)
        original_handlers=list(root.handlers)
        original_level=root.level
        captured=io.StringIO()
        handler=logging.StreamHandler(captured)
        root.addHandler(handler)
        try:
            adapter=self.adapter('malformed-private'); adapter.start()
            self.assertTrue(adapter.turn('private malformed fixture',timeout=3).success)
            logging.getLogger('unrelated-control').warning('KEEP_OTHER_LOGGER')
            adapter.close()
            self.assertEqual(list(root.filters),original_filters)
            self.assertEqual(root.level,original_level)
            self.assertEqual(root.handlers,original_handlers+[handler])
            self.assertNotIn('PRIVATE_SCHEMA_SENTINEL',captured.getvalue())
            self.assertIn('KEEP_OTHER_LOGGER',captured.getvalue())
            self.assertEqual(adapter._reader_state.get('sdk_diagnostic'),'native_acp_sdk_diagnostic')
        finally:
            root.removeHandler(handler)


if __name__=='__main__': unittest.main()
