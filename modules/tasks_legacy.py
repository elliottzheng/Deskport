# -*- coding: utf-8 -*-
"""任务-legacy 引擎：kimi -p stream-json（engine='p' 回退通道）。"""
import json
import logging
import os
import shutil
import subprocess
import threading

from modules import tasks_store as store
from modules import task_state

log = logging.getLogger('tasks')

def runTask(task, manifest):
    taskId = task['id']
    task_state.transition(task, task_state.TaskState.RUNNING)
    task['startedAt'] = store._now()
    store.repo.append_event(task, 'status', text='开始执行')
    kimi = shutil.which('kimi')
    if not kimi:
        task_state.transition(task, task_state.TaskState.FAILED)
        task['error'] = '找不到 kimi 命令'
        store.repo.append_event(task, 'error', text=task['error'])
        return
    cmd = [kimi, '-p', store._promptFor(task, manifest), '--output-format', 'stream-json']
    try:
        proc = subprocess.Popen(
            cmd, cwd=task['workspace'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=0x08000000 if os.name == 'nt' else 0)
    except OSError as e:
        task_state.transition(task, task_state.TaskState.FAILED)
        task['error'] = '启动 Kimi 失败: %s' % e
        store.repo.append_event(task, 'error', text=task['error'])
        return
    with store.repo.lock:
        store.repo.setRunning(taskId, proc)
    stderrLines = []

    def readStderr():
        try:
            for line in proc.stderr:
                stderrLines.append(line.rstrip())
                if len(stderrLines) > 200:
                    del stderrLines[:100]
        except Exception:
            pass

    errThread = threading.Thread(target=readStderr, daemon=True)
    errThread.start()

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                store.repo.append_event(task, 'log', text=line[:store.MAX_TEXT])
                continue
            role = obj.get('role')
            if role == 'meta':
                sid = obj.get('session_id')
                if sid:
                    task['sessionId'] = sid
                continue
            if role == 'assistant':
                calls = obj.get('tool_calls') or []
                for call in calls:
                    fn = call.get('function') or {}
                    store.repo.append_event(task, 'tool', name=fn.get('name') or '',
                                 args=(fn.get('arguments') or '')[:store.MAX_TEXT],
                                 callId=call.get('id') or '')
                content = obj.get('content')
                if content:
                    store.repo.append_event(task, 'assistant', text=str(content)[:store.MAX_TEXT])
                continue
            if role == 'tool':
                store.repo.append_event(task, 'tool_result',
                             text=str(obj.get('content') or '')[:store.MAX_TOOL_RESULT],
                             callId=obj.get('tool_call_id') or '')
                continue
            store.repo.append_event(task, 'log', text=line[:store.MAX_TEXT])
    except Exception as e:
        log.warning('read kimi stdout failed: %s', e)
    code = proc.wait()
    errThread.join(timeout=2)
    with store.repo.lock:
        store.repo.dropRunning(taskId)
    if task.get('status') == 'cancelled':
        task['endedAt'] = store._now()
        return
    with store.repo.lock:
        task['returncode'] = code
        task['endedAt'] = store._now()
    if code == 0:
        with store.repo.lock:
            task_state.transition(task, task_state.TaskState.DONE)
            store.repo.save()
        store.repo.append_event(task, 'status', text='任务完成')
    else:
        tail = '\n'.join(stderrLines[-20:]).strip()
        with store.repo.lock:
            task_state.transition(task, task_state.TaskState.FAILED)
            task['error'] = tail[-2000:] or ('Kimi 退出码 %s' % code)
            store.repo.save()
        store.repo.append_event(task, 'error', text=task.get('error') or '')
    results = store._scanResults(task['workspace'])
    with store.repo.lock:
        task['resultFiles'] = results
        store.repo.save()
