"""可操作的主线演示：从一次用药变化走到补问、跨会话恢复与重新复核。

它打印的是**页面读到的东西**（`GET /v1/safety-mainline` 与 `/v1/safety-cases` 的
真实返回），不是内部日志——所以"用户看到什么"和"系统做了什么"可以用同一份输出对照。

在隔离的临时库上跑，不碰 `stage0/memory.db`，不调真实模型，合成药名没有临床含义。

    python scripts/safety-mainline-demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from stage0.agent import DDITool, MedicationCoordinatorAgent  # noqa: E402
from stage0.memory import MemoryStore  # noqa: E402
from stage0.product import ProductStore  # noqa: E402
from stage0.safety_cases import SafetyCaseStore  # noqa: E402
from stage0 import safety_cases as sc  # noqa: E402

WARNING = {
    "drug_a": "合成药甲", "drug_b": "合成药乙", "severity": "major", "mechanism": "合成机制",
    "effect": "合成风险效应", "management": None,
    "source_text": "合成药甲与合成药乙合用可能增加合成风险（合成说明书文本）",
    "source_url": "https://synthetic.invalid/label/a", "confidence": "high",
    "detection_path": "synthetic_detector",
}


def detect(medications):
    return [dict(WARNING)] if {"合成药甲", "合成药乙"}.issubset(set(medications)) else []


class EmptyRAG:
    def __call__(self, query, **kwargs):
        return {"query": query, "mode": "synthetic", "results": []}


def show(step: str, title: str, body: dict | list | str) -> None:
    print(f'\n{"=" * 72}\n{step}  {title}\n{"=" * 72}')
    print(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, indent=2))


class Host:
    def __init__(self, directory: Path):
        import stage0.server as server
        holder: dict = {}

        def factory():
            return MedicationCoordinatorAgent(holder['store'], ddi_tool=DDITool(detect),
                                              rag_tool=EmptyRAG())

        self.app = server.create_app(db_path=directory / 'memory.db',
                                     worker_thread=False, agent_factory=factory)
        holder['store'] = self.app.state.store
        self.store = self.app.state.store
        self.worker = self.app.state.worker
        self.client = TestClient(self.app)
        self.product = ProductStore(self.store)

    def close(self):
        self.client.close()
        self.worker.stop()
        self.store.close()

    def change(self, key, name):
        body = {"session_id": "demo", "event_type": "medication_change",
                "text": f"新增{name}", "source": "caregiver",
                "payload": {"action": "add", "medication": name}}
        self.client.post('/v1/events', json=body, headers={'Idempotency-Key': key})
        self.worker.drain_once()

    def page(self):
        return self.client.get('/v1/safety-mainline').json()

    def cases(self):
        return self.client.get('/v1/safety-cases').json()['items']

    def card(self, case: dict) -> dict:
        """用户在一张事项卡片上真正看到的那几项。"""
        return {
            '事项状态': case['status_label'],
            '为什么产生': case['trigger'],
            '涉及记录': [item['label'] for item in case['medications']],
            '当前结论': [{'文本': c['text'], '状态': c['status'], '来源': c['sources']}
                        for c in case['conclusions']],
            '等待您回答': [i['question'] for i in case['required_inputs']],
            '下一步': case['next_action_summary'],
            '谁来做': case['responsible_party'],
        }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='safety-demo-') as directory:
        host = Host(Path(directory))
        try:
            show('①', '长期用药', '已有：合成药甲')
            host.change('demo-1', '合成药甲')
            show('②', '用户提交一次用药变化', '新增：合成药乙（经事件受理路径）')
            host.change('demo-2', '合成药乙')

            case = host.cases()[0]
            show('③', '必要检查执行并建立安全事项（未经模型）', host.card(case))

            # Agent 的调查登记在事项上；补问的**身份**由缺口派生，所以恢复后不会重问。
            store = SafetyCaseStore(host.product)
            store.require_input(case['case_id'], request_id=f"case:{case['case_id']}:schedule",
                                question='合成药乙目前的服用频次是什么？', fields=['schedule'])
            show('④', '调查登记与补问', host.card(host.cases()[0]))

            # 跨会话：关掉一切，从同一个数据库重开——还是同一件事、同一条问题。
            host.close()
            host = Host(Path(directory))
            resumed = host.cases()[0]
            show('⑤', '保存并结束进程后重开',
                 {'还是同一件事': resumed['case_id'] == case['case_id'],
                  '问题没有被重新生成': [i['request_id'] for i in resumed['required_inputs']]})

            store = SafetyCaseStore(host.product)
            store.record_input(case['case_id'], request_id=resumed['required_inputs'][0]['request_id'],
                               answer_ref='demo-answer', value='每日一次')
            show('⑥', '用户补充到达（只关闭它回答的那一条）',
                 {'状态': host.cases()[0]['status_label'],
                  '仍待确认': len(host.cases()[0]['required_inputs']),
                  '依据': host.cases()[0]['resolution_basis'],
                  '说明': '补充到达不等于事项解决；没有依据就不关闭。'})

            # 相关记录变化 → 旧依据失效 → 同一件事被重新打开，并说明为什么。
            host.change('demo-3', '合成药丙')
            reopened = host.cases()[0]
            show('⑦', '相关记录变化，旧判断失效',
                 {'还是同一件事': reopened['case_id'] == case['case_id'],
                  '状态': reopened['status_label'],
                  '为什么重新复核': reopened['next_action_summary'],
                  '历史里的重开记录': [e for e in reopened['history']
                                       if e['event'] in ('reopened', 'status_changed')][-2:]})

            show('⑧', '主页面的六块内容', {
                '当前用药': [m['display_name'] for m in host.page()['current_medications']],
                '需要关注': host.page()['counts']['attention'],
                '等待您补充': host.page()['counts']['awaiting_user'],
                '等待专业复核': host.page()['counts']['awaiting_professional'],
                '需要重新核对': host.page()['counts']['needs_recheck'],
                '最近已处理': host.page()['counts']['settled'],
                '必要检查队列': host.page()['necessary_checks'],
            })
            print('\n以上全部由代码路径产生；本演示未调用任何真实模型。')
        finally:
            host.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
