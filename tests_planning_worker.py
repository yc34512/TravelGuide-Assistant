"""子进程验收入口，所有请求由FixedModel的MockTransport处理。"""
import json
import os
import sys
from core.planning_runs import BudgetExhausted, RunStore
from pipeline.planning_graph import execute
from tests_planning import FixedModel

path, rid, mode, payload = sys.argv[1:]
store = RunStore(path)
def callback(*args):
    if mode == "crash_before_response":
        os._exit(78)
def hook(name, state):
    if mode == "crash_after_response" and name == "generate":
        os._exit(77)
pause = ["generate"] if mode == "pause_generate" else ["quality"] if mode == "pause_quality" else None
model = FixedModel(json.loads(payload), callback=callback)
try:
    with store.lease(rid):
        execute(store, rid, client_factory=model.factory, pause_after=pause, hook=hook)
except BudgetExhausted:
    sys.exit(2)
finally:
    model.close()
