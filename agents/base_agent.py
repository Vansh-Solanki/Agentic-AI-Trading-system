import logging
from abc import ABC, abstractmethod
from core.models import AgentOutput
from core.config import TradingMode


class BaseAgent(ABC):
    """Abstract base class for all trading agents."""

    def __init__(self, mode: TradingMode):
        self.mode = mode
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable agent name."""

    @abstractmethod
    def run(self, ticker: str, **kwargs) -> AgentOutput:
        """Execute the agent for a given ticker and return its output."""

    def _log_output(self, output: AgentOutput) -> None:
        self.logger.info(
            "[%s] %s — %d signals | %s",
            output.ticker,
            self.name,
            len(output.signals),
            output.summary[:120],
        )
