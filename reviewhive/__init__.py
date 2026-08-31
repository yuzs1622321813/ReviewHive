"""ReviewHive: 完全运行于本地模型的代码评审多 Agent 协作平台。"""
import os

__version__ = "0.1.0"

_LOCAL_HOSTS = ("127.0.0.1", "localhost")


def _ensure_local_no_proxy() -> None:
    """所有依赖服务都在本机：把本地地址强制加入 NO_PROXY，
    避免系统代理软件拦截回环连接（表现为间歇性 502 / 连接失败）。"""
    for var in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(var, "")
        hosts = [host for host in current.split(",") if host]
        missing = [host for host in _LOCAL_HOSTS if host not in hosts]
        if missing or (var == "NO_PROXY" and not current):
            os.environ[var] = ",".join(hosts + missing)


_ensure_local_no_proxy()
