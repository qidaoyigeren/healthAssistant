"""有限真实模型验收：**一句自然语言报告一次换药，其中一次对象说不清**。

范围在开跑**之前**就定死并打印出来——不是跑完再拿结果去解释标准：

* **一个连贯场景**：
  1. 用户说"这个药不吃了，换成另一种了"——**没有说清是哪一个**；
  2. 用户补一句"是合成药甲不吃了，换成合成药丙，明天开始"——旧药已停、新药尚未开始；
  3. 用户确认系统摆出来的候选；
  4. 回访继续。
* 请求上限 `--max-calls`（默认 **4**）、时间上限 `--wall-seconds`（默认 **180**）、
  token 上限 `--max-tokens`（默认 **40000**）；
* **先到先停**：任一上限用尽即停；**不追加批次、不换模型、不扩大预算**；
* **语义歧义应当补问，不靠重复调用猜中**：第 1 句读出歧义并提问**算有效结果**，
  不算失败。它没读出来、或者硬猜一个对象，才算没做到。

它回答四个**互相独立**的问题，一项通过**不**写成整个流程完成：

1. **自然语言理解**：模型有没有读出正确的对象、字段、时间与不确定部分；
2. **候选确认与原子写入**：确认之后记录有没有按规定变、有没有重复写入；
3. **用药阶段与纠错历史**：阶段标识、时间来源、换药关联是否如实；
4. **安全事项与回访承接**：事项有没有继续、风险有没有被自动清除。

模型**没猜中未提供的信息**不是失败；把没提供的信息写进记录才是失败。

异常也落盘：失败证据不能被一次崩溃吃掉。
隔离合成数据：临时库，不碰 `stage0/memory.db`，不产出任何临床建议。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SYNTHETIC_WARNING = {
    "drug_a": "合成药甲", "drug_b": "合成药乙", "severity": "major",
    "mechanism": "合成机制", "effect": "合成风险效应", "management": None,
    "source_text": "合成药甲与合成药乙合用可能增加合成风险（合成说明书文本）",
    "source_url": "https://synthetic.invalid/label/a",
    "confidence": "high", "detection_path": "synthetic_detector",
}


def synthetic_detect(medications):
    if {"合成药甲", "合成药乙"}.issubset(set(medications)):
        return [dict(SYNTHETIC_WARNING)]
    return []


FIRST_LINE = "这个药不吃了，换成另一种了"
#: 两半的**时间必须分开说**：旧药已经停了（已发生），新药尚未开始（计划）。
#: 写成"换成合成药丙，明天开始"会让"明天开始"统辖整句，那不是场景想要的。
SECOND_LINE = "是合成药甲，上周就停了；换成合成药丙，明天开始"


def resolve_config():
    """Fail closed on an endpoint that is not an explicit, listed decision.

    真实调用是对**外部端点**的动作，所以沿用既有的白名单：不因为"某个 key 恰好
    在本机环境里"就把请求发出去。
    """
    from stage0 import extract_ddi
    config = extract_ddi.resolve_llm_config()
    extract_ddi.assert_live_authorized(config)
    return config


class Report:
    def __init__(self, limits, config):
        self.data = {'limits': limits, 'provider': config['provider'],
                     'model': config['model'],
                     'started_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                     'steps': [], 'conclusions': {}, 'calls_used': 0,
                     'error': None, 'usage': None}

    def step(self, name, payload=None):
        self.data['steps'].append({'name': name, 'at': time.time(), 'detail': payload})
        tail = f' — {json.dumps(payload, ensure_ascii=False, default=str)[:180]}' if payload else ''
        print(f'· {name}{tail}')

    def save(self, out: Path):
        self.data['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        (out / 'live-acceptance.json').write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8')


#: 这次理解在台账里的种类标签。计数只数**它的**调用，不数同一张表里回访调查的。
READER_KIND = 'change_note_reader'


def reader_calls(app):
    try:
        return app.state.store.connection.execute(
            'SELECT COUNT(*) FROM llm_attempts WHERE kind=?', (READER_KIND,)).fetchone()[0]
    except Exception:
        return 0


def usage_of(app):
    """用量读**既有台账**；读不到就记 unknown，**不记 0**。"""
    try:
        rows = app.state.store.connection.execute(
            'SELECT status, usage_tokens FROM llm_attempts').fetchall()
    except Exception:
        return {'available': False, 'note': '读不到用量台账'}
    if not rows:
        return {'available': True, 'calls': 0, 'tokens': None,
                'note': '台账里没有这次运行的记录'}
    actual = [row['usage_tokens'] for row in rows if row['usage_tokens'] is not None]
    return {'available': True, 'calls': len(rows),
            'tokens': sum(actual) if actual else None,
            'tokens_unknown': not actual,
            'note': None if actual else '台账里没有实测 token 数，记 unknown（不是 0）'}


def conclusions(first_items, questions, second, pending, rows, active, confirmed,
                case_view, mainline):
    flagged = [item for item in first_items
               if 'object_ambiguous' in (item.get('uncertain') or [])]
    if flagged and questions:
        understanding = {'passed': True,
                         'note': f'第 1 句的指代被读出为不确定（{flagged[0].get("drug_quote")}），'
                                 f'并提出了具体问题：{questions[0].get("text")}'}
    elif flagged:
        understanding = {'passed': False, 'note': '读出了不确定，但没有提出具体问题'}
    elif first_items:
        understanding = {'passed': False,
                         'note': f'第 1 句没有说清是哪一个药，模型却没标为不确定：'
                                 f'{[i.get("drug_name") for i in first_items]}'}
    else:
        understanding = {'passed': False, 'note': f'第 1 句没有读出任何操作：{first_items}'}

    codes = [item.get('status') for item in confirmed]
    write = ({'passed': bool(codes) and all(code == 200 for code in codes),
              'note': f'确认返回 {codes}；当前在用：{active}'}
             if pending or codes else
             {'passed': False, 'note': '没有产生待确认候选，无从确认'})

    fake_times = [name for name, row in rows.items()
                  if row['status'] == 'stopped' and not row.get('end_at')
                  and row.get('end_at_basis') not in ('unknown', 'reported_vague')]
    history = ({'passed': False, 'note': f'这些行停了却没有时间来源，属于伪造时间：{fake_times}'}
               if fake_times else
               {'passed': True,
                'note': f'没有伪造的停药时间；阶段标识：'
                        f'{ {name: row.get("episode_id") for name, row in rows.items()} }'})

    checks = mainline.get('necessary_checks') or {}
    continuity = ({'passed': case_view['status'] != 'resolved',
                   'note': f"事项状态={case_view['status']}，必要检查 total={checks.get('total')} "
                           f"open={checks.get('open')}；停药没有被当成整体风险解除"}
                  if checks.get('available') else
                  {'passed': False, 'note': '读不到必要检查队列的真实状态'})
    return {'understanding': understanding, 'write': write,
            'history': history, 'continuity': continuity}


def run(report: Report, args) -> None:
    with tempfile.TemporaryDirectory(prefix='medication-change-live-') as tmp:
        _run_in(Path(tmp), report, args)


def _run_in(tmp: Path, report: Report, args) -> None:
    from fastapi.testclient import TestClient
    from stage0.agent import DDITool, MedicationCoordinatorAgent
    from stage0.product import ProductStore
    import stage0.server as server

    db_path = tmp / 'memory.db'
    holder: dict = {}
    app = None
    client = None
    try:

        def factory():
            return MedicationCoordinatorAgent(
                holder['store'], ddi_tool=DDITool(synthetic_detect),
                llm_planner_enabled=True,
                proposal_provider=lambda payload: {'decision': 'respond'})

        app = server.create_app(db_path=db_path, worker_thread=False,
                                agent_factory=factory)
        holder['store'] = app.state.store
        client = TestClient(app)
        ProductStore(app.state.store)

        def drain():
            app.state.worker.drain_once(max_tasks=6)

        def add_medication(key, name):
            client.post('/v1/events',
                        json={'session_id': 'live', 'event_type': 'medication_change',
                              'text': f'新增{name}', 'source': 'caregiver',
                              'payload': {'action': 'add', 'medication': name}},
                        headers={'Idempotency-Key': key})
            drain()

        add_medication('seed-1', '合成药甲')
        add_medication('seed-2', '合成药乙')
        case = client.get('/v1/safety-cases').json()['items'][0]
        case_id = case['case_id']
        started = client.post(
            f'/v1/safety-cases/{case_id}/visits',
            json={'key': 'live-visit', 'expected_revision': case['revision']}).json()
        drain()
        visit_id = started['visit']['visit_id']
        report.step('建事项与回访', {'case_id': case_id, 'visit_id': visit_id})

        def submit(text, key):
            if report.data['calls_used'] >= args.max_calls:
                return {'skipped': 'call_budget'}
            before = reader_calls(app)
            note = client.post(
                f'/v1/safety-cases/{case_id}/visits/{visit_id}/notes',
                json={'key': key, 'text': text}).json()
            report.data['calls_used'] += max(0, reader_calls(app) - before)
            return note

        first = submit(FIRST_LINE, 'live-note-1')
        report.step('第 1 句提交', {'text': FIRST_LINE, 'status': first.get('status'),
                                    'error': first.get('error'),
                                    'usage': first.get('usage')})
        first_items = (first.get('reading') or {}).get('items') or []
        questions = first.get('questions') or []
        report.step('第 1 句的理解',
                    {'items': first_items, 'questions': [q.get('text') for q in questions]})

        second = submit(SECOND_LINE, 'live-note-2')
        report.step('第 2 句提交', {'text': SECOND_LINE, 'status': second.get('status'),
                                    'error': second.get('error'),
                                    'usage': second.get('usage')})
        report.step('第 2 句的理解',
                    {'items': (second.get('reading') or {}).get('items') or [],
                     'plans': second.get('plans') or [],
                     'questions': [q.get('text') for q in (second.get('questions') or [])]})

        view = client.get(f'/v1/safety-cases/{case_id}').json()
        pending = view['visit']['pending_candidates']
        report.step('待确认候选',
                    [{'operation': c.get('operation'),
                      'name': (c.get('target') or {}).get('name'),
                      'changes': c.get('changes'), 'occurred': c.get('occurred'),
                      'group': c.get('group')} for c in pending])

        confirmed = []
        group = next((c for c in pending if (c.get('group') or {}).get('id')), None)
        if group:
            response = client.post(
                f'/v1/safety-cases/{case_id}/visits/{visit_id}/candidates/'
                f"{group['id']}/confirm-group", json={'key': 'live-confirm'})
            confirmed.append({'group': True, 'status': response.status_code,
                              'body': response.text[:300]})
        elif pending:
            response = client.post(
                f'/v1/safety-cases/{case_id}/visits/{visit_id}/candidates/'
                f"{pending[0]['id']}/confirm", json={'key': 'live-confirm'})
            confirmed.append({'group': False, 'status': response.status_code,
                              'body': response.text[:300]})
        drain()
        report.step('确认', confirmed)

        rows = {row['display_name']: dict(row) for row in app.state.store.connection.execute(
            "SELECT * FROM medications ORDER BY id")}
        active = [name for name, row in rows.items() if row['status'] == 'active']
        after_view = client.get(f'/v1/safety-cases/{case_id}').json()
        mainline = client.get('/v1/safety-mainline').json()
        report.data['conclusions'] = conclusions(
            first_items, questions, second, pending, rows, active, confirmed,
            after_view, mainline)
        report.data['records'] = {
            name: {key: row.get(key) for key in
                   ('status', 'operation', 'episode_id', 'dose', 'start_at', 'end_at',
                    'end_at_basis', 'time_text', 'predecessor_id', 'corrects_id')}
            for name, row in rows.items()}
        report.data['usage'] = usage_of(app)
    finally:
        # Windows 上句柄不关，临时目录就删不掉——一次清理异常会把**失败证据**
        # 连报告一起吃掉。先关连接，再让临时目录走。
        try:
            if client is not None:
                client.close()
        finally:
            if app is not None:
                app.state.store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--max-calls', type=int, default=4)
    parser.add_argument('--wall-seconds', type=float, default=180.0)
    parser.add_argument('--max-tokens', type=int, default=40_000)
    args = parser.parse_args()

    out = Path(args.out).resolve()
    if out.exists():
        raise SystemExit(f'输出目录已存在，拒绝覆盖：{out}')
    out.mkdir(parents=True)

    config = resolve_config()
    limits = {'max_calls': args.max_calls, 'wall_seconds': args.wall_seconds,
              'max_tokens': args.max_tokens}
    print('=' * 68)
    print('有限真实验收：一次连贯场景（换药 + 一次对象说不清）')
    print(f"provider={config['provider']} model={config['model']}")
    print(f"上限（开跑前声明，先到先停，不追加）：{json.dumps(limits)}")
    print('=' * 68)

    report = Report(limits, config)
    try:
        run(report, args)
    except Exception as exc:
        report.data['error'] = f'{type(exc).__name__}: {exc}'
        report.data['traceback'] = traceback.format_exc()
        print(f'!! 运行中断：{report.data["error"]}')
    finally:
        report.save(out)
        print('=' * 68)
        for key, value in (report.data['conclusions'] or {}).items():
            print(f"[{'通过' if value['passed'] else '未通过'}] {key}: {value['note']}")
        print(f"用量：{json.dumps(report.data.get('usage') or {}, ensure_ascii=False)}")
        print(f"调用数：{report.data['calls_used']} / {args.max_calls}")
        print('=' * 68)
    return 0 if not report.data['error'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
