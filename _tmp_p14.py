from tradingagents.dataflows.errors import (
    VendorError, VendorNotConfiguredError, VendorUnavailableError, NoMarketDataError,
)
from tradingagents.llm_clients.base_client import BaseLLMClient
from tradingagents.llm_clients.openai_client import (
    NormalizedChatOpenAI, DeepSeekChatOpenAI, LocalCompatibleChatOpenAI,
)
import tradingagents.screener.data_source as ds
from tradingagents.agents.state import AgentState

n = NoMarketDataError("600519.SS", "600519.SH", "stale")
print("A) NoMarketDataError 是 ValueError 吗:", isinstance(n, ValueError))
try:
    raise n
except ValueError:
    print("   被 ValueError 接住")
except VendorError as x:
    print("   被 VendorError 接住 ->", type(x).__name__)

e = VendorNotConfiguredError("missing key")
try:
    raise e
except ValueError as x:
    print("B) NotConfigured 被老调用方的 ValueError 接住 ->", type(x).__name__)

print("C) ABC 直接实例化:", end=" ")
try:
    BaseLLMClient("gpt-4o")
except TypeError as t:
    print("TypeError ->", t)

print("D) ChatClient MRO:", [c.__name__ for c in DeepSeekChatOpenAI.__mro__][:5])
print("E) issubclass Local->Normalized:", issubclass(LocalCompatibleChatOpenAI, NormalizedChatOpenAI))
print("F) removesuffix:", "OpenAIClient".removesuffix("Client").lower(),
      "|", "GoogleClient".removesuffix("Client").lower(),
      "|", "BedrockClient".removesuffix("Client").lower())
print("G) __qualname__/__module__:", type(e).__qualname__, "@", type(e).__module__)
print("H) screener 异常树:", issubclass(ds.DataSourceUnavailableError, ds.DataSourceError),
      issubclass(ds.NoDataError, ds.DataSourceError),
      isinstance(ds.NoDataError("x"), Exception),
      "| 与 VendorError 同源吗:", issubclass(ds.DataSourceError, VendorError))
print("I) AgentState 是 TypedDict -> 运行时就是 dict:", type(AgentState), isinstance({}, dict))
print("J) AgentState 注解键:", list(AgentState.__annotations__)[:6])
print("K) _前缀只是约定，外部照样能拿:", ds._compute_technical_indicators.__name__,
      callable(ds._compute_technical_indicators))
print("L) getattr 默认值:", getattr(e, "provider", None), "| hasattr symbol:", hasattr(n, "symbol"))
print("M) Exception 子类不写 __init__ 会继承:", VendorUnavailableError("throttled").args)
