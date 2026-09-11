"""Isolated, persistent synthetic fixture for actual process-restart acceptance."""
import argparse
import os
import time
from pathlib import Path

import uvicorn

from .server import create_app
from .test_agent_open_tasks import make_agent, seed, FixedRAG
from .agent import MedicationCoordinatorAgent, DDITool


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    parser.add_argument('--port', type=int, default=8039)
    parser.add_argument('--tool-delay-seconds', type=float, default=0,
                        help='Synthetic retrieval delay (0-10 seconds) for progress UI acceptance')
    args = parser.parse_args()
    if not 0 <= args.tool_delay_seconds <= 10:
        parser.error('Synthetic tool delay must be between 0 and 10 seconds')
    root = Path(__file__).resolve().parents[1]
    directory = Path(args.directory).resolve()
    if not directory.is_relative_to(root / 'output'):
        raise ValueError('Fixture must use an isolated output directory')
    directory.mkdir(parents=True, exist_ok=True)
    os.environ.update(MEMORY_ENABLE_LLM='0', AGENT_LLM_PLANNER='0', AGENT_INVESTIGATION_ENABLED='1',
                      AGENT_MULTI_REVIEW_MODEL_ENABLED='0', AGENT_GRAPH_RUNNER='1', STAGE0_REVIEW_ENABLED='0')
    class DelayedRAG(FixedRAG):
        def __call__(self, *call_args, **kwargs):
            time.sleep(args.tool_delay_seconds)
            return super().__call__(*call_args, **kwargs)
    def factory():
        return MedicationCoordinatorAgent(app.state.store, ddi_tool=DDITool(lambda _: []), rag_tool=DelayedRAG())
    app = create_app(db_path=directory / 'synthetic.db',
        checkpoint_path=str(directory / 'checkpoint.db'), agent_factory=factory,
        auth_mode='local-demo')
    if not app.state.store.current_medications():
        seed(app.state.store)

    @app.get('/v1/acceptance-fixture')
    def identity():
        return {'synthetic': True, 'pid': os.getpid(), 'directory': str(directory)}

    uvicorn.run(app, host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
