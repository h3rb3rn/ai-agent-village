import unittest
from unittest.mock import patch

from tests.test_runtime import RuntimeTests


class RuntimeResilienceTests(RuntimeTests):
    def test_unexpected_cycle_error_is_recorded_without_propagating(self):
        self.agent.env['OLLAMA_MODEL'] = 'synthetic:test'
        with patch.object(self.agent, 'cycle', side_effect=RuntimeError('synthetic cycle failure')):
            with patch('web.runtime.time.sleep', side_effect=lambda _: setattr(self.agent, 'stopping', True)):
                self.agent.run()
        self.assertEqual(self.agent.state.get('runtime_exception_streak'), 1)


if __name__ == '__main__':
    unittest.main()
