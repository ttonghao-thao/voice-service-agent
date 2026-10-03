from app.config import Settings
from app.contracts import AnswerBundle


class BusinessRuntime:
    """Bind a single executor at startup; requests cannot select a provider."""

    def __init__(self, settings: Settings, registry, profile=None):
        self.profile = profile or settings.execution_profile()
        if self.profile.mode == "direct":
            from app.agent_runtime.direct import DirectKnowledgeExecutor

            self.executor = DirectKnowledgeExecutor(settings, registry)
        else:
            from app.agent_runtime.external import ExternalLLMExecutor

            self.executor = ExternalLLMExecutor(settings, registry)
        self.registry = registry
        self.run = self.executor.run
        self.close = self.executor.close

    @property
    def client(self):
        return self.executor.client

    @property
    def validate(self):
        return self.executor.validate

    @staticmethod
    def failure(code, message):
        return AnswerBundle(status="failed", display_text=message, speech_text=message, reason_code=code)
